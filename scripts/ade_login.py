#!/usr/bin/env python3
"""Log in to AdE/SISTER on the shared CDP Chrome and leave the SISTER tab open.

The service no longer logs in at startup: it only attaches to the session created here
(``BrowserManager.attach_existing_session``). Run this whenever the session is missing or expired:

    ../.venv/bin/python scripts/ade_login.py          # log in (closes a stale SISTER session and retries once)
    ../.venv/bin/python scripts/ade_login.py --close  # only close the SISTER session held on the server

SISTER allows one session per user: a session left open (e.g. a crashed run) makes the next login fail with
"Utente gia' in sessione", and it has to be closed before logging in again.

Requires ``BROWSER_CDP_ENDPOINT`` plus the ``ADE_*`` credentials in ``.env``. Chrome is started by the
service (or via /web/browser/launch-chrome); if it is not reachable this script exits with an error.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT.parent / ".env", override=False)

from aecs4u_auth.browser import BrowserConfig  # noqa: E402
from aecs4u_auth.browser import BrowserManager as AuthBrowserManager  # noqa: E402
from aecs4u_auth.browser.exceptions import SessionLockedError  # noqa: E402

_CLOSE_SESSION_URLS = (
    "https://sister3.agenziaentrate.gov.it/Servizi/CloseSessionsSis",
    "https://sister3.agenziaentrate.gov.it/Servizi/CloseSessions",
)


async def close_stale_session(mgr: AuthBrowserManager) -> None:
    """Ask SISTER to drop the session it still holds for this user (same URLs the service uses)."""
    page = await mgr._context.new_page()
    try:
        for url in _CLOSE_SESSION_URLS:
            try:
                await page.goto(url, timeout=15000)
                await page.wait_for_load_state("networkidle", timeout=10000)
                print(f"Sessione SISTER chiusa via {url}")
            except Exception as e:
                print(f"{url}: {e}", file=sys.stderr)
    finally:
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
            await close_stale_session(mgr)
            return 0
        try:
            session = await mgr.login(service="sister")
        except SessionLockedError:
            print("Utente gia' in sessione: chiudo la sessione precedente e riprovo")
            await close_stale_session(mgr)
            session = await mgr.login(service="sister")
        print(f"Login SISTER completato: {session.page.url}")
    finally:
        # Disconnect only: closing the page/browser would drop the session the service attaches to.
        if mgr._playwright:
            await mgr._playwright.stop()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--close", action="store_true", help="only close the SISTER session held on the server")
    sys.exit(asyncio.run(main(parser.parse_args().close)))
