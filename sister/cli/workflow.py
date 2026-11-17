"""Commands for checking and cancelling server-side workflows."""

import json
from typing import Optional

import typer
from rich.markup import escape

from . import _runtime
from ._runtime import console

workflow_app = typer.Typer(
    name="workflow",
    help="Stato e annullamento dei workflow avviati con `query workflow --preset`",
    no_args_is_help=True,
)


def _workflow_call(method: str, workflow_id: str, output: Optional[str]) -> dict:
    result = _runtime.run_api(lambda client: getattr(client, method)(workflow_id))
    if output:
        _runtime.write_output(result, output)
    return result


@workflow_app.command("status")
def workflow_status(
    workflow_id: str = typer.Argument(help="ID del workflow (mostrato da `query workflow`)"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Percorso del file di output (.json)"),
):
    """Mostra lo stato di un workflow: passi completati, falliti, saltati."""
    result = _workflow_call("workflow_status", workflow_id, output)
    steps = result.get("steps") or []
    console.print(f"[bold]Workflow {escape(workflow_id)}[/bold] — {result.get('status', 'unknown')}")
    for step in steps:
        status = step.get("status", "unknown")
        style = "green" if status == "completed" else ("red" if status in {"error", "failed"} else "yellow")
        detail = f" {escape(str(step['error']))}" if step.get("error") else ""
        console.print(f"  [{style}]{step.get('step', '?')}[/{style}] — {status}{detail}")
    summary = result.get("summary") or {}
    if summary:
        console.print("  " + "  ".join(f"{key}: {value}" for key, value in summary.items()))
    if not steps and not summary:
        console.print(json.dumps(result, indent=2, ensure_ascii=False))


@workflow_app.command("cancel")
def workflow_cancel(
    workflow_id: str = typer.Argument(help="ID del workflow da annullare"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Percorso del file di output (.json)"),
):
    """Annulla un workflow in corso (i passi gia' eseguiti restano salvati)."""
    result = _workflow_call("cancel_workflow", workflow_id, output)
    console.print(f"[yellow]Workflow {escape(workflow_id)}[/yellow] — {result.get('status', 'cancel requested')}")
