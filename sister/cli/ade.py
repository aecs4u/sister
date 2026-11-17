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
    """Importa una pagina salvata dell'estratto AdE UTF-8 e aggiorna il relativo registro in euro."""
    from .. import ade_ledger

    async def _import():
        return await ade_ledger.import_statement_file(path)

    try:
        result = _runtime.run_db(_import)
    except (OSError, UnicodeError, ValueError) as exc:
        console.print(f"[red]Importazione dell'estratto non riuscita:[/red] {exc}")
        raise typer.Exit(1) from None
    if _runtime.json_output_enabled():
        _runtime.emit_data(result)
    else:
        console.print_json(json.dumps(result, ensure_ascii=False, default=str))


@ade_statement_app.command("link-pagopa")
def ade_statement_link_pagopa(
    statement_key: str = typer.Argument(..., help="Chiave radio della riga di ricarica AdE"),
    payment_id: int = typer.Argument(..., min=1, help="ID della riga pagoPA validata"),
):
    """Collega una ricarica alla riga pagoPA validata dopo aver verificato importo e data."""
    from .. import ade_ledger

    async def _link():
        await ade_ledger.link_validated_pagopa(statement_key, payment_id, linked_by=f"cli:{getpass.getuser()}"[:255])

    try:
        _runtime.run_db(_link)
    except (ValueError, RuntimeError) as exc:
        console.print(f"[red]Collegamento pagoPA non riuscito:[/red] {exc}")
        raise typer.Exit(1) from None
    if _runtime.json_output_enabled():
        _runtime.emit_data({"status": "linked", "statement_key": statement_key, "payment_id": payment_id})
    else:
        console.print("[green]Riga pagoPA validata collegata al movimento AdE.[/green]")


@ade_statement_app.command("detail-import")
def ade_statement_detail_import(
    statement_key: str = typer.Argument(..., help="Chiave radio della riga di addebito"),
    path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True),
):
    """Importa una pagina *Dettaglio* salvata; le pagine non riconosciute restano senza attribuzione."""
    from .. import ade_ledger

    async def _import():
        return await ade_ledger.import_statement_detail_file(statement_key, path)

    try:
        result = _runtime.run_db(_import)
    except (OSError, ValueError, RuntimeError) as exc:
        console.print(f"[red]Importazione del dettaglio non riuscita:[/red] {exc}")
        raise typer.Exit(1) from None
    if _runtime.json_output_enabled():
        _runtime.emit_data(result)
    else:
        console.print_json(json.dumps(result, ensure_ascii=False, default=str))


@ade_receipt_app.command("import")
def ade_receipt_import(path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True)):
    """Importa una quietanza AdE salvata in PDF o come testo nel registro separato delle evidenze."""
    from .. import ade_ledger

    async def _import():
        return await ade_ledger.import_receipt_file(path)

    try:
        result = _runtime.run_db(_import)
    except (OSError, UnicodeError, ValueError) as exc:
        console.print(f"[red]Importazione della quietanza non riuscita:[/red] {exc}")
        raise typer.Exit(1) from None
    if _runtime.json_output_enabled():
        _runtime.emit_data(result)
    else:
        console.print_json(json.dumps(result, ensure_ascii=False, default=str))
