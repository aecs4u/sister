#!/usr/bin/env python3
"""Login via CIE, find immobili, then extract intestati for a specific subalterno.

This demonstrates the full two-phase flow from the README:
  Phase 1: POST /visura    -> find immobili on a particella
  Phase 2: POST /intestati -> get owners for a specific subalterno

Usage:
    uv run python examples/login_and_intestati.py

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
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False, markup=True)],
)
log = logging.getLogger("example")
console = Console()

# --- Parameters (change these) ---
PROVINCIA = "Trieste"
COMUNE = "TRIESTE"
FOGLIO = "9"
PARTICELLA = "166"
SUBALTERNO = "3"  # Specifico fabbricato


async def main():
    from aecs4u_auth.browser import BrowserConfig, BrowserManager

    from utils import run_visura, run_visura_immobile

    os.environ.setdefault("ADE_AUTH_METHOD", "cie")

    config = BrowserConfig()
    console.rule(f"[bold]Login via {config.auth_method.upper()}[/bold]")

    manager = BrowserManager(config)
    try:
        await manager.initialize()

        log.info("Approva la richiesta CIE sul tuo dispositivo (timeout %ds)...", config.mfa_timeout_seconds)
        session = await manager.login(service="sister")
        log.info("[green]Login completato![/green]")

        await manager.start_keepalive()
        page = session.page

        # --- Phase 1: Find immobili ---
        console.rule("[bold]Fase 1 — Ricerca immobili (Fabbricati)[/bold]")

        result = await run_visura(
            page,
            provincia=PROVINCIA,
            comune=COMUNE,
            foglio=FOGLIO,
            particella=PARTICELLA,
            tipo_catasto="F",
            extract_intestati=False,
        )

        immobili = result.get("immobili", [])
        if not immobili:
            console.print("[yellow]Nessun immobile trovato — verifica i parametri[/yellow]")
            return

        table = Table(title=f"Fabbricati su F.{FOGLIO} P.{PARTICELLA}")
        for col in immobili[0].keys():
            table.add_column(col)
        for row in immobili:
            table.add_row(*[str(v) for v in row.values()])
        console.print(table)

        # --- Phase 2: Get intestati for a specific subalterno ---
        console.rule(f"[bold]Fase 2 — Intestati Sub.{SUBALTERNO}[/bold]")

        result_int = await run_visura_immobile(
            page,
            provincia=PROVINCIA,
            comune=COMUNE,
            foglio=FOGLIO,
            particella=PARTICELLA,
            subalterno=SUBALTERNO,
        )

        intestati = result_int.get("intestati", [])
        if not intestati:
            console.print("[yellow]Nessun intestato trovato per Sub.{SUBALTERNO}[/yellow]")
        else:
            table = Table(title=f"Intestati Sub.{SUBALTERNO}")
            for col in intestati[0].keys():
                table.add_column(col)
            for row in intestati:
                table.add_row(*[str(v) for v in row.values()])
            console.print(table)

        # --- Summary ---
        console.print(
            Panel(
                f"[green]{len(immobili)}[/green] immobili trovati, "
                f"[green]{len(intestati)}[/green] intestati per Sub.{SUBALTERNO}",
                title="Riepilogo",
            )
        )

    finally:
        log.info("Shutdown...")
        await manager.graceful_shutdown()
        log.info("[green]Sessione chiusa[/green]")


if __name__ == "__main__":
    asyncio.run(main())
