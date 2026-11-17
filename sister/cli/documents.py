"""Document maintenance commands that need no separate tool process."""

import json
from pathlib import Path

import typer

from . import _runtime
from ._runtime import console
from .tools import documents_app


@documents_app.command("import-receipts")
def documents_import_receipts(path: Path = typer.Argument(..., exists=True, readable=True)):
    """Import one AdE receipt or all receipt PDFs/TXT files in a directory."""
    from .. import ade_ledger

    paths = [path] if path.is_file() else sorted(
        child for child in path.iterdir() if child.is_file() and child.suffix.casefold() in {".pdf", ".txt", ".text"}
    )
    if not paths:
        console.print("[yellow]Nessuna ricevuta PDF/TXT trovata.[/yellow]")
        raise typer.Exit(1)

    async def _import_all():
        results = []
        for source in paths:
            try:
                results.append({"file": source.name, **(await ade_ledger.import_receipt_file(source))})
            except Exception as exc:  # noqa: BLE001 - continue the directory batch and report each source outcome
                results.append({"file": source.name, "error": str(exc)[:200]})
        return results

    results = _runtime.run_db(_import_all)
    console.print_json(json.dumps(results, ensure_ascii=False, default=str))
    if any("error" in item for item in results):
        raise typer.Exit(1)
