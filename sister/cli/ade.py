"""Direct commands for the Agenzia delle Entrate ledger."""

import getpass
import json
from pathlib import Path

import typer

from . import _runtime
from ._runtime import console

ade_app = typer.Typer(name="ade", help="Import e riconciliazione del conto AdE separato dai wallet SISTER", no_args_is_help=True)
ade_statement_app = typer.Typer(name="statement", help="Estratto conto consultazioni AdE", no_args_is_help=True)
ade_receipt_app = typer.Typer(name="receipt", help="Quietanze AdE", no_args_is_help=True)
ade_app.add_typer(ade_statement_app, name="statement")
ade_app.add_typer(ade_receipt_app, name="receipt")


@ade_statement_app.command("import")
def ade_statement_import(path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True)):
    """Import a saved UTF-8 AdE statement page and rebuild its dedicated euro ledger."""
    from .. import ade_ledger

    async def _import():
        return await ade_ledger.import_statement_file(path)

    try:
        result = _runtime.run_db(_import)
    except (OSError, UnicodeError, ValueError) as exc:
        console.print(f"[red]Import statement non riuscito:[/red] {exc}")
        raise typer.Exit(1) from None
    console.print_json(json.dumps(result, ensure_ascii=False, default=str))


@ade_statement_app.command("link-pagopa")
def ade_statement_link_pagopa(
    statement_key: str = typer.Argument(..., help="Chiave radio della riga di ricarica AdE"),
    payment_id: int = typer.Argument(..., min=1, help="ID della riga pagoPA validata"),
):
    """Link a top-up to a validated pagoPA source row after exact amount/date checks."""
    from .. import ade_ledger

    async def _link():
        await ade_ledger.link_validated_pagopa(statement_key, payment_id, linked_by=f"cli:{getpass.getuser()}"[:255])

    try:
        _runtime.run_db(_link)
    except (ValueError, RuntimeError) as exc:
        console.print(f"[red]Collegamento pagoPA non riuscito:[/red] {exc}")
        raise typer.Exit(1) from None
    console.print("[green]Riga pagoPA validata collegata al movimento AdE.[/green]")


@ade_statement_app.command("detail-import")
def ade_statement_detail_import(
    statement_key: str = typer.Argument(..., help="Chiave radio della riga di addebito"),
    path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True),
):
    """Import a saved *Dettaglio* page; unrecognized pages remain unattributed."""
    from .. import ade_ledger

    async def _import():
        return await ade_ledger.import_statement_detail_file(statement_key, path)

    try:
        result = _runtime.run_db(_import)
    except (OSError, ValueError, RuntimeError) as exc:
        console.print(f"[red]Import dettaglio non riuscito:[/red] {exc}")
        raise typer.Exit(1) from None
    console.print_json(json.dumps(result, ensure_ascii=False, default=str))


@ade_receipt_app.command("import")
def ade_receipt_import(path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True)):
    """Import a saved AdE receipt PDF or extracted text into the separate ledger evidence store."""
    from .. import ade_ledger

    async def _import():
        return await ade_ledger.import_receipt_file(path)

    try:
        result = _runtime.run_db(_import)
    except (OSError, UnicodeError, ValueError) as exc:
        console.print(f"[red]Import ricevuta non riuscito:[/red] {exc}")
        raise typer.Exit(1) from None
    console.print_json(json.dumps(result, ensure_ascii=False, default=str))
