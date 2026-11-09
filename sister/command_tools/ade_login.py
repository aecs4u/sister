#!/usr/bin/env python3
"""Log in to AdE/SISTER on the shared CDP Chrome and leave the SISTER tab open.

The service no longer logs in at startup: it only attaches to the session created here
(``BrowserManager.attach_existing_session``). Run this whenever the session is missing or expired:

    uv run sister app login          # log in (closes a stale SISTER session and retries once)
    uv run sister app login --close  # only close the SISTER session held on the server

SISTER allows one session per user: a session left open (e.g. a crashed run) makes the next login fail with
"Utente gia' in sessione", and it has to be closed before logging in again.

Requires ``BROWSER_CDP_ENDPOINT`` plus the ``ADE_*`` credentials in ``.env``. Chrome is started by the
service (or via /web/browser/launch-chrome); if it is not reachable this script exits with an error.
"""

import argparse
import asyncio
import json
import sys
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT.parent / ".env", override=False)

from aecs4u_auth.browser import BrowserConfig  # noqa: E402
from aecs4u_auth.browser import BrowserManager as AuthBrowserManager  # noqa: E402
from aecs4u_auth.browser.exceptions import SessionLockedError  # noqa: E402
from aecs4u_auth.browser.services import get_service  # noqa: E402

# when the last login succeeded: SISTER ends a session ~30 minutes after it, whatever the activity, so `batch run-session`
# times its clean stop from here instead of from the moment it is started
LOGIN_RECORD = ROOT / "logs" / "sister_login.json"


def record_login(url: str) -> None:
    try:
        LOGIN_RECORD.parent.mkdir(parents=True, exist_ok=True)
        LOGIN_RECORD.write_text(
            json.dumps({"logged_in_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "url": url}),
            encoding="utf-8",
        )
    except OSError as exc:  # the login itself worked: only the timing hint is lost
        print(f"ora del login non salvata ({exc})", file=sys.stderr)


def clear_login_record() -> None:
    LOGIN_RECORD.unlink(missing_ok=True)


_CLOSE_SESSION_URLS = (
    "https://sister3.agenziaentrate.gov.it/Servizi/CloseSessionsSis",
    "https://sister3.agenziaentrate.gov.it/Servizi/CloseSessions",
)
_SISTER_VISURE_URL = "https://sister3.agenziaentrate.gov.it/Visure/SceltaServizio.do?tipo=/T/TM/VCVC_"


async def close_stale_session(mgr: AuthBrowserManager) -> bool:
    """Close the SISTER session and verify that Visure redirects to login."""
    page = await mgr._context.new_page()
    try:
        for url in _CLOSE_SESSION_URLS:
            try:
                await page.goto(url, timeout=15000)
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception as e:
                print(f"{url}: {e}", file=sys.stderr)

        try:
            await page.goto(_SISTER_VISURE_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            if await get_service("sister").check_session(page):
                print("La sessione SISTER risulta ancora attiva.", file=sys.stderr)
                return False

            current_url = page.url.lower()
            title = (await page.title()).lower()
            is_login_page = (
                "login" in current_url
                or "login" in title
                or "iampe.agenziaentrate.gov.it" in current_url
            )
            if not is_login_page:
                print(
                    f"Impossibile verificare la chiusura della sessione SISTER (pagina: {page.url}).",
                    file=sys.stderr,
                )
                return False

            print("Sessione SISTER chiusa o già assente; reindirizzamento al login verificato.")
            return True
        except Exception as e:
            print(f"Verifica della sessione SISTER non riuscita: {e}", file=sys.stderr)
            return False
    finally:
        with suppress(Exception):
            await page.close()


async def main(close_only: bool) -> int:
    config = BrowserConfig()
    if not config.cdp_endpoint:
        print("BROWSER_CDP_ENDPOINT non impostato: il login condiviso richiede Chrome via CDP", file=sys.stderr)
        return 2

    mgr = AuthBrowserManager(config)
    await mgr.initialize()
    try:
        if close_only:
            if not await close_stale_session(mgr):
                return 1
            clear_login_record()
            return 0
        try:
            session = await mgr.login(service="sister")
        except SessionLockedError:
            print("Utente gia' in sessione: chiudo la sessione precedente e riprovo")
            if not await close_stale_session(mgr):
                print(
                    "Login interrotto: impossibile verificare la chiusura della sessione precedente.",
                    file=sys.stderr,
                )
                return 1
            session = await mgr.login(service="sister")

        if not await get_service("sister").check_session(session.page):
            print(f"Login SISTER non verificato (pagina: {session.page.url}).", file=sys.stderr)
            clear_login_record()
            return 1

        print(f"Login SISTER completato: {session.page.url}")
        record_login(session.page.url)
    finally:
        # Disconnect only: closing the page/browser would drop the session the service attaches to.
        if mgr._playwright:
            await mgr._playwright.stop()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--close", action="store_true", help="only close the SISTER session held on the server")
    sys.exit(asyncio.run(main(parser.parse_args().close)))
