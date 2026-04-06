#!/usr/bin/env python3
"""Login via CIE and run a visura catastale on SISTER.

Usage:
    # Default: CIE authentication with Sielte provider
    uv run python examples/login_and_visura.py

    # Override via environment
    ADE_AUTH_METHOD=spid ADE_SPID_PROVIDER=aruba uv run python examples/login_and_visura.py

Requires:
    - ADE_USERNAME and ADE_PASSWORD in .env or environment
    - Playwright chromium installed: uv run playwright install chromium
"""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

# Rich logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False, markup=True)],
)
log = logging.getLogger("example")
console = Console()


async def main():
    from aecs4u_auth.browser import BrowserConfig, BrowserManager

    from utils import run_visura

    # --- Configuration ---
    # Default to CIE authentication; override via env vars
    os.environ.setdefault("ADE_AUTH_METHOD", "cie")

    config = BrowserConfig()
    console.rule(f"[bold]Login via {config.auth_method.upper()}[/bold]")
    log.info("Username: %s", config.username)
    log.info("Auth method: [cyan]%s[/cyan]", config.auth_method)
    if config.auth_method == "spid":
        log.info("SPID provider: [cyan]%s[/cyan]", config.spid_provider)

    # --- Browser & Login ---
    manager = BrowserManager(config)
    try:
        await manager.initialize()
        log.info("Browser inizializzato")

        console.rule("[bold]Autenticazione in corso[/bold]")
        log.info("Approva la richiesta CIE sul tuo dispositivo (timeout %ds)...", config.mfa_timeout_seconds)
        session = await manager.login(service="sister")
        log.info("[green]Login completato![/green]")

        # --- Keep-alive ---
        await manager.start_keepalive()

        # --- Visura: Fabbricati a Trieste ---
        console.rule("[bold]Visura Fabbricati[/bold]")

        page = session.page
        result = await run_visura(
            page,
            provincia="Trieste",
            comune="TRIESTE",
            foglio="9",
            particella="166",
            tipo_catasto="F",
            extract_intestati=False,
        )

        # --- Display results ---
        immobili = result.get("immobili", [])
        if not immobili:
            console.print("[yellow]Nessun immobile trovato[/yellow]")
        else:
            table = Table(title=f"Immobili trovati: {len(immobili)}")
            if immobili:
                for col in immobili[0].keys():
                    table.add_column(col)
                for row in immobili:
                    table.add_row(*[str(v) for v in row.values()])
            console.print(table)

        # --- Visura: Terreni a Roma ---
        console.rule("[bold]Visura Terreni[/bold]")

        result_t = await run_visura(
            page,
            provincia="Roma",
            comune="ROMA",
            foglio="100",
            particella="50",
            tipo_catasto="T",
            extract_intestati=True,
        )

        immobili_t = result_t.get("immobili", [])
        intestati_t = result_t.get("intestati", [])

        if immobili_t:
            table = Table(title=f"Terreni trovati: {len(immobili_t)}")
            for col in immobili_t[0].keys():
                table.add_column(col)
            for row in immobili_t:
                table.add_row(*[str(v) for v in row.values()])
            console.print(table)

        if intestati_t:
            table = Table(title=f"Intestati: {len(intestati_t)}")
            for col in intestati_t[0].keys():
                table.add_column(col)
            for row in intestati_t:
                table.add_row(*[str(v) for v in row.values()])
            console.print(table)

        console.rule("[green bold]Completato[/green bold]")

    finally:
        log.info("Shutdown...")
        await manager.graceful_shutdown()
        log.info("[green]Sessione chiusa[/green]")


if __name__ == "__main__":
    asyncio.run(main())
