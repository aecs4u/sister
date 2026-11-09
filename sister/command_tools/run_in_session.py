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


def start_service(port: int, log: Path) -> subprocess.Popen:
    if health(port):  # an old service (own state, maybe a dead session) would be mistaken for the one started here
        raise SystemExit(f"la porta {port} e' gia' occupata da un altro servizio: fermalo (o usa --port) e rilancia")
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", action="append", required=True, type=Path, help="plan directory (repeat, run in order)")
    parser.add_argument("--minutes", type=float, default=26, help="minutes after the login to stop (SISTER closes at ~30)")
    parser.add_argument("--logged-in-at", help="HH:MM of the login (default: the time `sister app login` recorded, else now)")
    parser.add_argument("--grace-seconds", type=float, default=60,
                        help="after the deadline no row starts; one still running this long after it is interrupted")
    parser.add_argument("--port", type=int, default=8027)
    parser.add_argument("--preflight-cf", default=os.getenv("SISTER_PREFLIGHT_CF", ""), help="code for the proof search")
    parser.add_argument("--cdp", default=os.getenv("BROWSER_CDP_ENDPOINT", "http://localhost:9222"))
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
        say("[red]la sessione e' quasi finita: rifai il login con sister app login e rilancia[/red]")
        return 2

    answered = Answered() if args.keep_answered else asyncio.run(answered_queries())
    for directory in args.batch:
        kept, done, invalid = prune_plan(directory, answered, write=not args.dry_run)
        say(f"{directory.name}: {kept} righe da fare ({done} gia' risposte tolte, {invalid} codici fiscali non validi tolti)")
    if args.dry_run:
        return 0

    if not live_session(args.cdp) or (args.probe and not session_is_alive(args.cdp)):
        say("[red]nessuna sessione SISTER utilizzabile: fai il login con sister app login (prima --close) e rilancia[/red]")
        return 2
    if args.probe:
        say("la sessione e' valida (prova con una scheda a parte)")
    service = None
    outcome = "not_started"

    def _terminate(signum, _frame):  # a closed terminal (SIGHUP) or a kill (SIGTERM) must still stop the service below
        raise KeyboardInterrupt

    for _sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(_sig, _terminate)
    try:
        for attempt in (1, 2):
            try:
                service = start_service(args.port, args.batch[0] / "service.log")
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
            if not preflight(args.port, args.preflight_cf, args.batch[0] / "preflight.json"):
                say("[red]la ricerca di verifica e' fallita: la sessione non e' utilizzabile[/red]")
                return 3
            say("verifica reale della sessione: ok")
        dashboard = Dashboard(args.batch, login, deadline)
        quiet = {"last": 0.0}

        with Live(dashboard.view(), console=console, refresh_per_second=4, transient=False) as live:

            def tick() -> None:
                if console.is_terminal:
                    live.update(dashboard.view())
                elif time.time() - quiet["last"] > 60:  # no terminal (log file): one summary line a minute
                    quiet["last"] = time.time()
                    snaps = dashboard.snaps()
                    say(f"avanzamento {sum(s['done'] for s in snaps)}/{sum(s['total'] for s in snaps)}")

            for directory in args.batch:
                say(f"avvio {directory.name}")
                outcome = run_batch(directory, args.port, deadline, tick, grace=args.grace_seconds)
                say(f"{directory.name}: {outcome}")
                if outcome != "done":
                    break
    except KeyboardInterrupt:
        outcome = "interrupted"
    finally:
        since = batch_requests_since(args.batch)
        if service is not None and outcome in {"done", "deadline", "interrupted"} and since:
            say(f"controllo i documenti pronti in Richieste dal {since}")
            try:
                count = download_ready_documents(args.port, since)
                say(f"Richieste: scaricati {count} documenti nuovi (quelli gia' salvati sono stati saltati)")
            except Exception as exc:  # Keep the extraction result even if the portal document pass fails.
                say(f"[yellow]download Richieste non completato: {exc}[/yellow]")
        stop(service)
        say("servizio fermo: nessun login automatico possibile")

    console.print(render([snapshot(directory) for directory in args.batch]))  # final state, after the service stopped
    if outcome == "done":
        say("tutti i piani sono finiti")
    else:
        say("per riprendere: nuovo login con sister app login, poi rilancia lo stesso comando (--resume salta il fatto)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
