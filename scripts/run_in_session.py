#!/usr/bin/env python3
"""Run `sister query batch` plans inside ONE SISTER session and close cleanly before the portal does.

SISTER ends a session about 30 minutes after the login, whatever the activity. This script, started right after the login:

1. checks that the shared Chrome shows a live SISTER page (otherwise: log in with ``scripts/ade_login.py``);
2. starts the service (no reload) and waits until it is attached to that session;
3. drops from every plan the rows that already have an answer in the database, and the codici fiscali whose check
   character is wrong (SISTER rejects them), so nothing answered is asked twice;
4. proves the session with one real search;
5. runs the plans in order (``query batch --resume``) until ``--minutes`` after the login, or until the portal reports an
   expired session, then interrupts the batch (the journal keeps every state, ``--resume`` continues) and stops the service,
   which therefore never gets the chance to open a login of its own;
6. prints the progress of each plan and what to do next.

    ../.venv/bin/python scripts/run_in_session.py --batch <dir> [--batch <dir> ...] [--minutes 26] [--logged-in-at 17:43]
    ../.venv/bin/python scripts/run_in_session.py --batch <dir> --dry-run          # plan pruning and timing only

Each ``<dir>`` holds ``batch.json`` (the plan) and gets ``batch_journal.jsonl`` and ``run.log``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from batch_progress import Dashboard, render, snapshot  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.live import Live  # noqa: E402

from sister.fiscal_code import valid_codice_fiscale  # noqa: E402

console = Console()
EXPIRED = re.compile(r"sessione_scaduta|lock\.html|Sessione SISTER scaduta")
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
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            return json.load(response)
    except Exception:  # noqa: BLE001 (not up yet)
        return {}


def start_service(port: int, log: Path) -> subprocess.Popen:
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


async def answered_codes() -> set[str]:
    """Codes SISTER already answered for Fabbricati and Terreni (results, or its own "no match"), and those whose visura document is stored.

    An answer to a search limited to Terreni or Fabbricati does not count: "no match" there only says the subject has none of that kind.
    """
    from sqlalchemy import text

    from sister.database import _get_session_factory

    async with _get_session_factory()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT subject_query FROM request WHERE subject_query IS NOT NULL "
                    "AND (cadastre_type IS NULL OR cadastre_type = 'E') "
                    "AND (success IS TRUE OR error ILIKE '%NESSUNA CORRISPONDENZA%')"
                )
            )
        ).scalars().all()
        # "Superato il limite massimo di 400 elementi": SISTER's final answer too, whatever the catasto type (the view reads the code
        # from the job payload, the answer carries no subject)
        too_many = (
            await session.execute(text("SELECT fiscal_code FROM v_subject_search_outcome WHERE outcome = 'too_many'"))
        ).scalars().all()
        files = (await session.execute(text("SELECT file_name FROM document WHERE file_name ~* '^vs_'"))).scalars().all()
    codes = {str(code).strip().upper() for code in [*rows, *too_many]}
    for name in files:
        match = re.match(r"vs_[a-z]+_([A-Z0-9]{11,16})_", name or "", re.I)
        if match:
            codes.add(match.group(1).upper())
    return codes


def prune_plan(directory: Path, answered: set[str]) -> tuple[int, int, int]:
    """Rewrite ``batch.json`` without the answered rows and the invalid codici fiscali. Returns (kept, answered, invalid)."""
    path = directory / "batch.json"
    plan = json.loads(path.read_text(encoding="utf-8"))
    kept, done, invalid = [], 0, 0
    for query in plan.get("queries", []):
        code = str((query.get("params") or {}).get("codice_fiscale") or "").strip().upper()
        if code and code in answered:
            done += 1
        elif len(code) == 16 and not valid_codice_fiscale(code):
            invalid += 1
        else:
            if (query.get("command") or plan.get("defaults", {}).get("command")) == "soggetto-immobili":
                query.setdefault("params", {}).setdefault("tipo_catasto", "E")  # Fabbricati and Terreni
            kept.append(query)
    plan["queries"] = kept
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(kept), done, invalid


# -- running ------------------------------------------------------------------------------------------------------


def cli_env(port: int) -> dict:
    env = dict(os.environ)
    env["VISURA_API_URL"] = f"http://localhost:{port}"
    if os.getenv("API_KEY"):
        env["VISURA_API_KEY"] = os.environ["API_KEY"]
    return env


def preflight(port: int, code: str, out: Path) -> bool:
    """One real search: reading the open page is not proof that the portal still honours the session."""
    command = [sys.executable, "-m", "sister.cli", "query", "soggetto", "--cf", code, "--wait", "--yes", "-o", str(out)]
    try:
        subprocess.run(command, cwd=ROOT, env=cli_env(port), capture_output=True, timeout=120)
        return json.loads(out.read_text(encoding="utf-8")).get("status") == "completed"
    except Exception:  # noqa: BLE001
        return False


def run_batch(directory: Path, port: int, deadline: datetime, tick=None) -> str:
    """Run one plan until it ends, the deadline passes or the portal reports an expired session: 'done'|'deadline'|'expired'.

    ``tick`` is called about every second while it runs (the progress bar).
    """
    log = directory / "run.log"
    start = log.stat().st_size if log.exists() else 0
    command = [
        sys.executable, "-m", "sister.cli", "query", "batch", "-I", str(directory / "batch.json"), "--resume",
        "-o", str(directory / "report.json"),
    ]
    with open(log, "a") as handle:
        process = subprocess.Popen(command, cwd=ROOT, env=cli_env(port), stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    reason = "done"
    while process.poll() is None:
        if datetime.now() >= deadline:
            reason = "deadline"
        elif log.exists() and EXPIRED.search(log.read_bytes()[start:].decode("utf-8", "replace")):
            reason = "expired"
        if reason != "done":
            os.killpg(process.pid, signal.SIGINT)  # the journal keeps every state; --resume continues
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
            break
        for _ in range(4):  # the bar moves every half second, the log is read every two
            if tick:
                tick()
            time.sleep(0.5)
    if tick:
        tick()
    return reason


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", action="append", required=True, type=Path, help="plan directory (repeat, run in order)")
    parser.add_argument("--minutes", type=float, default=26, help="minutes after the login to stop (SISTER closes at ~30)")
    parser.add_argument("--logged-in-at", help="HH:MM of the login (default: now)")
    parser.add_argument("--port", type=int, default=8027)
    parser.add_argument("--preflight-cf", default=os.getenv("SISTER_PREFLIGHT_CF", ""), help="code for the proof search")
    parser.add_argument("--cdp", default=os.getenv("BROWSER_CDP_ENDPOINT", "http://localhost:9222"))
    parser.add_argument("--dry-run", action="store_true", help="prune the plans and print the timing, start nothing")
    parser.add_argument("--keep-answered", action="store_true", help="do not drop the rows that already have an answer (to repeat codes with another "
                        "'Tipo richiesta': the database does not record it, so an Attualità answer would hide the Storica row)")
    parser.add_argument("--probe", action="store_true", help="also open a scratch tab to test the login first. Off by default: a second tab on the live "
                        "session made SISTER answer lock.html to the search that followed (2026-10-27); the service attach and the proof search are the test")
    args = parser.parse_args()

    now = datetime.now()
    if args.logged_in_at:
        hour, minute = map(int, args.logged_in_at.split(":"))
        login = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    else:
        login = now
    deadline = login + timedelta(minutes=args.minutes)
    say(f"login {login:%H:%M}, chiusura pulita alle {deadline:%H:%M} ({(deadline - now).total_seconds() / 60:.0f} min da ora)")
    if deadline <= now + timedelta(minutes=2):
        say("[red]la sessione e' quasi finita: rifai il login con scripts/ade_login.py e rilancia[/red]")
        return 2

    answered = set() if args.keep_answered else asyncio.run(answered_codes())
    for directory in args.batch:
        kept, done, invalid = prune_plan(directory, answered)
        say(f"{directory.name}: {kept} righe da fare ({done} gia' risposte tolte, {invalid} codici fiscali non validi tolti)")
    if args.dry_run:
        return 0

    if not live_session(args.cdp) or (args.probe and not session_is_alive(args.cdp)):
        say("[red]nessuna sessione SISTER utilizzabile: fai il login con scripts/ade_login.py (prima --close) e rilancia[/red]")
        return 2
    if args.probe:
        say("la sessione e' valida (prova con una scheda a parte)")
    service = None
    outcome = "done"
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
                outcome = run_batch(directory, args.port, deadline, tick)
                say(f"{directory.name}: {outcome}")
                if outcome != "done":
                    break
    except KeyboardInterrupt:
        outcome = "interrupted"
    finally:
        stop(service)
        say("servizio fermo: nessun login automatico possibile")

    console.print(render([snapshot(directory) for directory in args.batch]))  # final state, after the service stopped
    if outcome == "done":
        say("tutti i piani sono finiti")
    else:
        say("per riprendere: nuovo login con scripts/ade_login.py, poi rilancia lo stesso comando (--resume salta il fatto)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
