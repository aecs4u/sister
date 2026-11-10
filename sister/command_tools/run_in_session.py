#!/usr/bin/env python3
"""Run `sister query batch` plans inside ONE SISTER session and close cleanly before the portal does.

SISTER ends a session about 30 minutes after the login, whatever the activity. This script, started right after the login:

1. checks that the shared Chrome shows a live SISTER page (otherwise: log in with ``sister app login``);
2. starts the service (no reload) and waits until it is attached to that session;
3. drops from every plan the rows that already have an answer in the database, and the codici fiscali whose check
   character is wrong (SISTER rejects them), so nothing answered is asked twice;
4. optionally proves the session with one real search (``--preflight-cf``) before starting the batch;
5. runs the plans in order (``query batch --resume --stop-at``): no row starts after ``--minutes`` from the login (taken from
   ``sister app login``'s record, ``--logged-in-at`` or now), the batch stops by itself at the first expired-session answer,
   and only a row still running ``--grace-seconds`` after the deadline is interrupted (the journal keeps every state,
   ``--resume`` continues);
6. downloads ready, not-yet-stored documents from SISTER's Richieste page, starting from the earliest request in the batch;
7. stops the service, which therefore never gets the chance to open a login of its own, and prints progress and next steps.

    uv run sister batch run-session --batch <dir> [--batch <dir> ...] [--minutes 26] [--logged-in-at 17:43]
    uv run sister batch run-session --batch <dir> --dry-run          # plan pruning and timing only

Each ``<dir>`` holds ``batch.json`` (the plan) and gets ``batch_journal.jsonl`` and ``run.log``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from batch_progress import Dashboard, render, snapshot  # noqa: E402
from run_list_status import RunListStatus  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.live import Live  # noqa: E402

from sister.answered import Answered, row_code  # noqa: E402
from sister.fiscal_code import valid_codice_fiscale  # noqa: E402

console = Console()
EXIT_SESSION_EXPIRED = 75  # sister.cli.EXIT_SESSION_EXPIRED (not imported: the CLI module loads the whole app)
LOGIN_RECORD = ROOT / "logs" / "sister_login.json"  # written by `sister app login` (ade_login.record_login)
SESSION_MINUTES = 30
DEAD_PAGE = ("login.jsp", "sessione_scaduta", "Logout", "lock.html", "about:blank")


def say(message: str) -> None:
    console.print(f"[bold]{datetime.now():%H:%M:%S}[/bold] {message}")


# -- browser and service ------------------------------------------------------------------------------------------


def sister_pages(cdp: str) -> list[dict]:
    with urllib.request.urlopen(f"{cdp}/json", timeout=5) as response:
        return [t for t in json.load(response) if t.get("type") == "page" and "sister3.agenziaentrate.gov.it" in t.get("url", "")]


def live_session(cdp: str) -> bool:
    try:
        return any(not any(mark in page["url"] for mark in DEAD_PAGE) for page in sister_pages(cdp))
    except OSError:
        return False


async def _probe(cdp: str) -> bool:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(cdp)
        page = await browser.contexts[0].new_page()
        try:
            await page.goto("https://sister3.agenziaentrate.gov.it/Visure/SceltaServizio.do?tipo=/T/TM/VCVC_", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            return not any(mark in page.url for mark in DEAD_PAGE) and await page.locator("select[name='listacom'] option").count() > 1
        except Exception:  # noqa: BLE001
            return False
        finally:
            await page.close()  # only the probe tab: closing it does not touch the login or the user's tabs


def session_is_alive(cdp: str) -> bool:
    """A real test of the login: a scratch tab asks SISTER for the office page. The URL or age of the open tab proves nothing
    (a leftover page of an ended session looks the same)."""
    try:
        return asyncio.run(_probe(cdp))
    except Exception:  # noqa: BLE001
        return False


def health(port: int) -> dict:
    # /health shows the auth state only to the API key; an anonymous probe gets {"status": ...} and never "ready".
    headers = {"X-API-Key": os.environ["API_KEY"]} if os.getenv("API_KEY") else {}
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/health", headers=headers)
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)
    except Exception:  # noqa: BLE001 (not up yet)
        return {}


def port_owner(port: int) -> str:
    """Who listens on ``port`` ('pid 123 (uvicorn), started 09:07'), or '' when it cannot be told."""
    try:
        out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True, timeout=5).stdout
        pids = sorted({int(m) for m in re.findall(r"pid=(\d+)", out)})
        if not pids:
            return ""
        started = subprocess.run(["ps", "-o", "lstart=", "-p", str(pids[0])], capture_output=True, text=True, timeout=5).stdout.strip()
        return f"pid {', '.join(map(str, pids))}" + (f", avviato {started}" if started else "")
    except Exception:  # noqa: BLE001 (diagnostics only)
        return ""


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def stop_stale_service(port: int, wait: float = 15.0) -> str:
    """Stop the SISTER service left on ``port`` by an earlier run; return '' when done or the reason it was not stopped.

    Only a ``sister.main:app`` uvicorn is touched, and only when no other batch run is going (a ``run_in_session`` or
    ``query batch`` process of someone else would lose its service). Chrome and the SISTER login are not affected.
    """
    out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True, timeout=5).stdout
    pids = sorted({int(m) for m in re.findall(r"pid=(\d+)", out)})
    if not pids:
        return f"nessun processo in ascolto sulla porta {port}"
    for pid in pids:
        if "sister.main:app" not in _cmdline(pid):
            return f"il pid {pid} non e' un servizio SISTER ({_cmdline(pid)[:60]}): non lo fermo"
    mine = {os.getpid(), os.getppid()}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and int(entry.name) not in mine:
            cmd = _cmdline(int(entry.name))
            if ("sister.command_tools.run_in_session" in cmd or "sister.cli query batch" in cmd) and "--dry-run" not in cmd:
                return f"c'e' un batch in corso (pid {entry.name}): fermalo prima"
    targets = set(pids)
    for pid in pids:  # the `uv run` wrapper is the parent: leaving it would restart nothing, but leave a zombie shell
        try:
            parent = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
            if "uvicorn" in _cmdline(parent) and "sister.main:app" in _cmdline(parent):
                targets.add(parent)
        except (OSError, ValueError, IndexError):
            pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in targets:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        deadline = time.time() + wait
        while time.time() < deadline:
            if not health(port):
                return ""
            time.sleep(0.5)
    return f"il servizio sulla porta {port} non si e' fermato"


def start_service(port: int, log: Path, stop_stale: bool = False) -> subprocess.Popen:
    if health(port) and stop_stale:
        owner = port_owner(port)
        reason = stop_stale_service(port)
        if reason:
            raise SystemExit(f"servizio sulla porta {port} non fermato: {reason}")
        print(f"servizio rimasto sulla porta {port} fermato ({owner})")
    if health(port):  # an old service (own state, maybe a dead session) would be mistaken for the one started here
        owner = port_owner(port)
        hint = f" ({owner}; se non c'e' un batch in corso: kill {' '.join(re.findall(r'pid ([\d, ]+)', owner)[0].replace(',', '').split())})" if owner else ""
        raise SystemExit(f"la porta {port} e' gia' occupata da un altro servizio{hint}: fermalo (o usa --port, o --stop-stale-service) e rilancia")
    handle = open(log, "w")
    process = subprocess.Popen(
        [str(ROOT / "scripts" / "start.sh"), "--port", str(port), "--no-reload"],
        cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
    )
    for _ in range(30):
        data = health(port)
        state = (data.get("auth") or {}).get("state")
        if state == "ready":
            return process
        if process.poll() is not None:
            raise SystemExit(f"il servizio e' terminato subito: vedi {log}")
        time.sleep(2)
    stop(process)
    raise SystemExit(f"il servizio non si e' agganciato alla sessione entro 1 minuto: vedi {log}")


def stop(process: subprocess.Popen | None, grace: float = 10.0) -> None:
    """Stop a process group: a normal signal, then a forced one if it ignores it (a service waiting on a login does)."""
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    except ProcessLookupError:
        pass


# -- plans --------------------------------------------------------------------------------------------------------


async def answered_queries() -> Answered:
    """What SISTER already answered, per kind of query (rules: ``sister.answered``).

    Searches: requests over Fabbricati and Terreni that found something or got "nessuna corrispondenza", and "oltre 400" for a
    code. Documents: answered ``soggetto-documento`` requests (their view and the provinces requested) and the stored ``vs_``
    files. An answer limited to Terreni or Fabbricati does not count: "no match" there only says the subject has none of that kind.
    """
    from sqlalchemy import text

    from sister.database import _get_session_factory

    async with _get_session_factory()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT upper(btrim(subject_query)), request_ref LIKE 'soggetto\\_documento%', success IS TRUE, id "
                    "FROM request WHERE subject_query IS NOT NULL "
                    "AND (cadastre_type IS NULL OR cadastre_type = 'E') "
                    "AND (success IS TRUE OR error ILIKE '%NESSUNA CORRISPONDENZA%') "
                    # an earlier persona giuridica answer that is only the list of registry records (or nothing at all) says
                    # nothing about the properties: the code still has to be searched
                    "AND NOT (success IS TRUE AND request_ref LIKE 'pnf\\_%' AND EXISTS ("
                    "SELECT 1 FROM sister_archive.request_payload p WHERE p.request_id = request.id "
                    "AND p.data NOT LIKE '%omonimi_totale%' AND p.data NOT LIKE '%visImmSel%'))"
                )
            )
        ).all()
        document_ids = [row_id for _, is_document, found, row_id in rows if is_document and found]
        payloads = (
            (
                await session.execute(
                    text("SELECT request_id, data FROM sister_archive.request_payload WHERE request_id = ANY(:ids)"),
                    {"ids": document_ids},
                )
            ).all()
            if document_ids
            else []
        )
        # "Superato il limite massimo di 400 elementi": SISTER's final answer too, whatever the catasto type (the view reads the code
        # from the job payload, the answer carries no subject)
        too_many = (
            await session.execute(text("SELECT fiscal_code FROM v_subject_search_outcome WHERE outcome = 'too_many'"))
        ).scalars().all()
        files = (await session.execute(text("SELECT file_name FROM document WHERE file_name ~* '^vs_'"))).scalars().all()
    # "oltre 400" is final for a code (nothing narrows it) but not for a name: those are split by province of the seat
    too_many = [code for code in too_many if len(str(code).strip()) in (11, 16) and str(code).strip().isalnum()]
    codes = {row_id: code for code, _, _, row_id in rows}
    documents = []
    for row_id, data in payloads:
        try:
            payload = json.loads(data) if isinstance(data, str) else dict(data or {})
        except (TypeError, ValueError):
            payload = {}
        documents.append((codes[row_id], payload.get("vista"), payload.get("richieste") or []))
    return Answered.build(
        searches=[code for code, is_document, _, _ in rows if not is_document] + too_many,
        no_match=[code for code, _, found, _ in rows if not found],
        documents=documents,
        files=files,
    )


def prune_plan(directory: Path, answered: Answered | set[str], *, write: bool = True) -> tuple[int, int, int]:
    """Rewrite ``batch.json`` without the answered rows and the invalid codici fiscali. Returns (kept, answered, invalid).

    "Answered" depends on the row's command (``sister.answered``): an elenco does not answer a document request. The previous
    plan is copied to ``backups/`` first; ``write=False`` only counts.
    """
    if not isinstance(answered, Answered):
        answered = Answered.build(searches=answered)
    path = directory / "batch.json"
    plan = json.loads(path.read_text(encoding="utf-8"))
    defaults = plan.get("defaults") or {}
    kept, done, invalid = [], 0, 0
    for query in plan.get("queries", []):
        params = query.get("params") or {}
        command = query.get("command") or defaults.get("command")
        code = row_code(params)
        forced = query.get("reuse") == "force"  # repeated on purpose: it stays even though SISTER already answered it
        if not forced and answered.covers(command, {**(defaults.get("params") or {}), **params}):
            done += 1
        elif len(code) == 16 and not valid_codice_fiscale(code):
            invalid += 1
        else:
            if command == "soggetto-immobili":
                query.setdefault("params", {}).setdefault("tipo_catasto", "E")  # Fabbricati and Terreni
            kept.append(query)
    if write and (done or invalid or kept != plan.get("queries", [])):
        backup = directory / "backups" / f"batch.{datetime.now():%Y%m%d_%H%M%S}.json"
        backup.parent.mkdir(exist_ok=True)
        shutil.copy2(path, backup)
        plan["queries"] = kept
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(kept), done, invalid


# -- running ------------------------------------------------------------------------------------------------------


def recorded_login(path: Path = LOGIN_RECORD, now: datetime | None = None) -> datetime | None:
    """Local time of the last ``sister app login``, when it can still be the live session (less than 30 minutes ago)."""
    try:
        stamp = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["logged_in_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    login = stamp.astimezone().replace(tzinfo=None) if stamp.tzinfo else stamp
    now = now or datetime.now()
    return login if timedelta(0) <= now - login < timedelta(minutes=SESSION_MINUTES) else None


def cli_env(port: int) -> dict:
    env = dict(os.environ)
    env["VISURA_API_URL"] = f"http://localhost:{port}"
    if os.getenv("API_KEY"):
        env["VISURA_API_KEY"] = os.environ["API_KEY"]
    return env


def batch_requests_since(directories: list[Path]) -> str:
    """Return the date of the earliest submitted request recorded in these batch journals."""
    dates: list[str] = []
    for directory in directories:
        journal = directory / "batch_journal.jsonl"
        if not journal.is_file():
            continue
        for line in journal.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                stamp = entry.get("updated_at")
                if not entry.get("request_ids") or not stamp:
                    continue
                dates.append(datetime.fromisoformat(stamp.replace("Z", "+00:00")).date().isoformat())
            except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
                continue
    return min(dates) if dates else ""


def download_ready_documents(port: int, since: str) -> int:
    """Import ready, not-yet-stored documents from SISTER's Richieste page."""
    query = urllib.parse.urlencode({"since": since, "skip_known": "true"})
    headers = {}
    api_key = os.getenv("API_KEY") or os.getenv("VISURA_API_KEY")
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/visura/download-documents?{query}",
        data=b"",
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        result = json.load(response)
    if result.get("status") != "completed":
        raise RuntimeError(f"Risposta inattesa da download Richieste: {result.get('status')!r}")
    return int(result.get("total_documents") or 0)


def preflight(port: int, code: str, out: Path) -> bool:
    """One real search: reading the open page is not proof that the portal still honours the session."""
    command = [sys.executable, "-m", "sister.cli", "query", "soggetto", "--cf", code, "--wait", "--yes", "-o", str(out)]
    try:
        subprocess.run(command, cwd=ROOT, env=cli_env(port), capture_output=True, timeout=120)
        return json.loads(out.read_text(encoding="utf-8")).get("status") == "completed"
    except Exception:  # noqa: BLE001
        return False


def run_batch(directory: Path, port: int, deadline: datetime, tick=None, *, grace: float = 60.0) -> str:
    """Run one plan until it ends, the deadline passes or the portal reports an expired session: 'done'|'deadline'|'expired'|'failed'.

    The batch itself starts no row after ``deadline`` (``--stop-at``: the running row finishes and is journaled) and stops
    at the first expired-session answer (exit code ``EXIT_SESSION_EXPIRED``). Only a row still running ``grace`` seconds
    after the deadline is interrupted. ``tick`` is called about every second while it runs (the progress bar).
    """
    log = directory / "run.log"
    command = [
        sys.executable, "-m", "sister.cli", "query", "batch", "-I", str(directory / "batch.json"), "--resume",
        "-o", str(directory / "report.json"), "--stop-at", deadline.isoformat(timespec="seconds"),
    ]
    started_at = time.time()
    with open(log, "a") as handle:
        process = subprocess.Popen(command, cwd=ROOT, env=cli_env(port), stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    hard_stop = deadline + timedelta(seconds=grace)
    while process.poll() is None:
        if datetime.now() >= hard_stop:
            os.killpg(process.pid, signal.SIGINT)  # the journal keeps every state; --resume continues
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
            break
        if tick:
            tick()
        time.sleep(0.5)
    if tick:
        tick()
    if process.returncode == EXIT_SESSION_EXPIRED:
        return "expired"
    report = directory / "report.json"
    if process.returncode not in (0, None) and not (report.exists() and report.stat().st_mtime >= started_at - 1):
        return "failed"  # the batch crashed before writing its report (see run.log): nothing was processed
    if datetime.now() >= deadline and _stopped_at_deadline(directory):
        return "deadline"
    return "deadline" if datetime.now() >= hard_stop else "done"


def _stopped_at_deadline(directory: Path) -> bool:
    try:
        return json.loads((directory / "report.json").read_text(encoding="utf-8")).get("stopped") == "stop_at"
    except (OSError, ValueError):
        return True  # interrupted before the report was written


def progress_email_content(
    snaps: list[dict],
    initial_snaps: list[dict],
    outcome: str,
    login: datetime,
    deadline: datetime,
) -> tuple[str, str, str]:
    """Build a plain-text and HTML summary of this run's final batch progress."""
    from html import escape

    total = sum(s["total"] for s in snaps)
    done = sum(s["done"] for s in snaps)
    initial_done = sum(s["done"] for s in initial_snaps)
    subject = f"SISTER run-list: {done}/{total} risposte ({outcome})"
    lines = [
        "SISTER run-list session summary",
        f"Esito sessione: {outcome}",
        f"Login: {login:%Y-%m-%d %H:%M}",
        f"Chiusura prevista: {deadline:%Y-%m-%d %H:%M}",
        f"Totale: {done}/{total} risposte finali ({max(0, done - initial_done)} nuove in questa sessione)",
        "",
    ]
    batch_rows = []
    for snap, initial in zip(snaps, initial_snaps):
        new_done = max(0, snap["done"] - initial["done"])
        lines.extend(
            [
                f"{snap['name']} ({snap['dir']}): {snap['done']}/{snap['total']} risposte finali; "
                f"{new_done} nuove in questa sessione",
                f"  Trovate: {snap['found']} | nessuna corrispondenza: {snap['no_match']} | "
                f"criteri da affinare: {snap['refine']} | errori: {snap['failed']} | "
                f"sessione scaduta: {snap['session']} | in attesa: {snap['pending']}",
            ]
        )
        batch_rows.append(
            "<tr>"
            f"<td>{escape(str(snap['name']))} ({escape(str(snap['dir']))})</td>"
            f"<td>{snap['done']}/{snap['total']}</td>"
            f"<td>{new_done}</td>"
            f"<td>{snap['found']}</td><td>{snap['no_match']}</td><td>{snap['refine']}</td>"
            f"<td>{snap['failed']}</td><td>{snap['session']}</td><td>{snap['pending']}</td>"
            "</tr>"
        )
    html = (
        "<h2>SISTER run-list session summary</h2>"
        f"<p>Esito: {escape(outcome)}<br>Login: {login:%Y-%m-%d %H:%M}"
        f"<br>Chiusura prevista: {deadline:%Y-%m-%d %H:%M}</p>"
        f"<p>Totale: {done}/{total} risposte finali; {max(0, done - initial_done)} nuove in questa sessione.</p>"
        "<table border='1' cellpadding='5' cellspacing='0'><thead><tr>"
        "<th>Lotto</th><th>Risposte</th><th>Nuove in sessione</th><th>Trovate</th>"
        "<th>Nessuna corrispondenza</th><th>Criteri da affinare</th><th>Errori</th>"
        "<th>Sessione scaduta</th><th>In attesa</th></tr></thead><tbody>"
        + "".join(batch_rows)
        + "</tbody></table>"
    )
    return subject, "\n".join(lines), html


def send_progress_email(
    recipient: str,
    snaps: list[dict],
    initial_snaps: list[dict],
    outcome: str,
    login: datetime,
    deadline: datetime,
) -> tuple[bool, str]:
    """Send the final session summary through the configured aecs4u-email provider."""
    subject, text_body, html_body = progress_email_content(snaps, initial_snaps, outcome, login, deadline)

    async def send() -> tuple[bool, str]:
        from aecs4u_email import send_email

        result = await send_email(
            to=recipient,
            subject=subject,
            html=html_body,
            text=text_body,
            tags=["sister-run-list-progress"],
            metadata={"outcome": outcome, "batch_count": len(snaps)},
        )
        return bool(result.success), str(result.provider)

    return asyncio.run(send())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", action="append", required=True, type=Path, help="plan directory (repeat, run in order)")
    parser.add_argument("--minutes", type=float, default=26, help="minutes after the login to stop (SISTER closes at ~30)")
    parser.add_argument("--logged-in-at", help="HH:MM of the login (default: the time `sister app login` recorded, else now)")
    parser.add_argument("--grace-seconds", type=float, default=60,
                        help="after the deadline no row starts; one still running this long after it is interrupted")
    parser.add_argument("--port", type=int, default=8027)
    parser.add_argument("--preflight-cf", default=os.getenv("SISTER_PREFLIGHT_CF", ""), help="code for the proof search")
    parser.add_argument("--stop-stale-service", action="store_true", help="if the port is held by a SISTER service left by an earlier run "
                        "(and no batch is running), stop it and start a fresh one; Chrome and the login are not touched")
    parser.add_argument("--cdp", default=os.getenv("BROWSER_CDP_ENDPOINT", "http://localhost:9222"))
    parser.add_argument("--email-progress", action="store_true", help="email the final progress summary when this session run closes")
    parser.add_argument("--email-to", default="demo@aecs4u.com", help="progress summary recipient (default: demo@aecs4u.com)")
    parser.add_argument("--dry-run", action="store_true", help="count what pruning would drop and print the timing; change and start nothing")
    parser.add_argument("--keep-answered", action="store_true", help="do not drop the rows that already have an answer (rows asking for the Storica are "
                        "always kept: the database does not record the 'Tipo richiesta' of an answer)")
    parser.add_argument("--probe", action="store_true", help="also open a scratch tab to test the login first. Off by default: a second tab on the live "
                        "session made SISTER answer lock.html to the search that followed (2026-10-27); use --preflight-cf for a real proof search")
    args = parser.parse_args()

    now = datetime.now()
    if args.logged_in_at:
        hour, minute = map(int, args.logged_in_at.split(":"))
        login = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    elif (recorded := recorded_login()) is not None:
        login = recorded
        say(f"ora del login registrata da sister app login: {login:%H:%M}")
    else:
        login = now
        say("[yellow]nessun login registrato: conto i minuti da adesso (se il login e' di prima, usa --logged-in-at HH:MM)[/yellow]")
    deadline = login + timedelta(minutes=args.minutes)
    say(f"login {login:%H:%M}, chiusura pulita alle {deadline:%H:%M} ({(deadline - now).total_seconds() / 60:.0f} min da ora)")
    if deadline <= now + timedelta(minutes=2):
        if not args.dry_run:
            tracker = RunListStatus(args.batch)
            tracker.start()
            tracker.set_phase("checking_session")
            tracker.finish("session_unavailable")
        say("[red]la sessione e' quasi finita: rifai il login con sister app login e rilancia[/red]")
        return 2

    tracker = None if args.dry_run else RunListStatus(args.batch)
    if tracker is not None:
        tracker.start()
        tracker.set_phase("pruning_plan")
    try:
        answered = Answered() if args.keep_answered else asyncio.run(answered_queries())
        for directory in args.batch:
            kept, done, invalid = prune_plan(directory, answered, write=not args.dry_run)
            say(f"{directory.name}: {kept} righe da fare ({done} gia' risposte tolte, {invalid} codici fiscali non validi tolti)")
    except Exception:
        if tracker is not None:
            tracker.finish("failed")
        raise
    if args.dry_run:
        return 0

    assert tracker is not None
    service = None
    outcome = "not_started"
    session_started = False
    initial_snaps: list[dict] = []

    def _terminate(signum, _frame):  # a closed terminal (SIGHUP) or a kill (SIGTERM) must still stop the service below
        raise KeyboardInterrupt

    for _sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(_sig, _terminate)
    try:
        tracker.set_phase("checking_session")
        if not live_session(args.cdp) or (args.probe and not session_is_alive(args.cdp)):
            outcome = "session_unavailable"
            say("[red]nessuna sessione SISTER utilizzabile: fai il login con sister app login (prima --close) e rilancia[/red]")
            return 2
        if args.probe:
            say("la sessione e' valida (prova con una scheda a parte)")

        tracker.set_phase("starting_service")
        for attempt in (1, 2):
            try:
                service = start_service(args.port, args.batch[0] / "service.log", args.stop_stale_service)
                break
            except SystemExit as exc:
                # right after a login the first page is not the Visure one: the first start only brings the tab there
                if attempt == 2:
                    raise
                say(f"aggancio non riuscito ({exc}): secondo tentativo")
        say("servizio agganciato alla sessione")
        if not args.preflight_cf:
            say("[yellow]nessuna ricerca di verifica (--preflight-cf o SISTER_PREFLIGHT_CF): la sessione e' provata solo "
                "dall'aggancio del servizio[/yellow]")
        if args.preflight_cf:
            tracker.set_phase("preflight")
            if not preflight(args.port, args.preflight_cf, args.batch[0] / "preflight.json"):
                outcome = "preflight_failed"
                say("[red]la ricerca di verifica e' fallita: la sessione non e' utilizzabile[/red]")
                return 3
            say("verifica reale della sessione: ok")
        session_started = True
        outcome = "running"
        tracker.set_phase("running")
        initial_snaps = [snapshot(directory) for directory in args.batch]
        dashboard = Dashboard(args.batch, login, deadline)
        quiet = {"last": 0.0}
        last_status = {"heartbeat": time.monotonic()}

        with Live(dashboard.view(), console=console, refresh_per_second=4, transient=False) as live:

            def tick() -> None:
                if time.monotonic() - last_status["heartbeat"] >= 15:
                    tracker.heartbeat()
                    last_status["heartbeat"] = time.monotonic()
                if console.is_terminal:
                    live.update(dashboard.view())
                elif time.time() - quiet["last"] > 60:  # no terminal (log file): one summary line a minute
                    quiet["last"] = time.time()
                    snaps = dashboard.snaps()
                    say(f"avanzamento {sum(s['done'] for s in snaps)}/{sum(s['total'] for s in snaps)}")

            for directory in args.batch:
                say(f"avvio {directory.name}")
                tracker.set_phase("running_batch", directory.name)
                outcome = run_batch(directory, args.port, deadline, tick, grace=args.grace_seconds)
                say(f"{directory.name}: {outcome}")
                if outcome != "done":
                    break
    except KeyboardInterrupt:
        outcome = "interrupted"
    except SystemExit:
        if outcome == "not_started":
            outcome = "start_failed"
        raise
    except Exception:
        outcome = "failed"
        raise
    finally:
        since = batch_requests_since(args.batch)
        if service is not None and outcome in {"done", "deadline", "interrupted"} and since:
            tracker.set_phase("downloading_documents")
            say(f"controllo i documenti pronti in Richieste dal {since}")
            try:
                count = download_ready_documents(args.port, since)
                say(f"Richieste: scaricati {count} documenti nuovi (quelli gia' salvati sono stati saltati)")
            except Exception as exc:  # Keep the extraction result even if the portal document pass fails.
                say(f"[yellow]download Richieste non completato: {exc}[/yellow]")
        tracker.set_phase("stopping_service")
        stop(service)
        say("servizio fermo: nessun login automatico possibile")
        if args.email_progress and session_started:
            try:
                final_snaps = [snapshot(directory) for directory in args.batch]
                sent, provider = send_progress_email(args.email_to, final_snaps, initial_snaps, outcome, login, deadline)
                if sent and provider == "console":
                    say("[yellow]email elaborata dal provider console, ma non consegnata[/yellow]")
                elif sent and provider == "disabled":
                    say("[yellow]invio email disabilitato dalla configurazione aecs4u-email[/yellow]")
                elif sent:
                    say(f"email di riepilogo inviata a {args.email_to}")
                else:
                    say("[yellow]email di riepilogo non inviata: il provider ha segnalato un errore[/yellow]")
            except Exception as exc:  # Email failure must not change the batch outcome.
                say(f"[yellow]email di riepilogo non inviata ({type(exc).__name__})[/yellow]")
        tracker.finish(outcome)

    console.print(render([snapshot(directory) for directory in args.batch]))  # final state, after the service stopped
    if outcome == "done":
        say("tutti i piani sono finiti")
    else:
        say("per riprendere: nuovo login con sister app login, poi rilancia lo stesso comando (--resume salta il fatto)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
