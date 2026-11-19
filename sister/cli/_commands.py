"""CLI for the SISTER cadastral visura service.

Provides subcommands to submit cadastral searches, poll for results,
query history, and check service health.

Usage:
    sister query search -P Trieste -C TRIESTE -F 9 -p 166
    sister query intestati -P Trieste -C TRIESTE -F 9 -p 166 -t F --subalterno 3
    sister workflow run --preset due-diligence -P Trieste -C TRIESTE -F 9 -p 166 -t F
    sister query batch -I parcels.csv --wait
    sister query batch -I batch.json -O outputs/run1 [--resume]
    sister requests get <request_id>
    sister requests wait <request_id>
    sister requests list --status pending
    sister requests list --provincia Trieste --limit 20
    sister health
"""

import os
from pathlib import Path
from typing import Optional

import typer
from ..environment import load_environment
from rich.table import Table

from ..client import VisuraClient
from ..query_forms import QUERY_FORMS
from . import _runtime
from . import query as _query
from .ade import ade_app
from .app_tools import app_tools_app
from .audit_tools import audit_app
from .batch_tools import batch_app
from .build_tools import build_app
from .db import db_app
from .dev import dev_app
from .documents import documents_app
from .mortgage import mortgage_app
from .render import render_health
from .requests import register_request_commands
from .workflow import workflow_app

query_app = _query.query_app


app = typer.Typer(
    name="sister",
    help="CLI del servizio di visure catastali SISTER",
    epilog="Codici di uscita: 0 successo; 1 richiesta non riuscita; 2 input non valido; 3 ambiente o servizio non disponibile; 75 sessione scaduta; 130 interruzione.",
    no_args_is_help=True,
)
app.add_typer(query_app, name="query", rich_help_panel="Service")
register_request_commands(app)

app.add_typer(db_app, name="db", rich_help_panel="Database")

app.add_typer(workflow_app, name="workflow", rich_help_panel="Service")

app.add_typer(mortgage_app, name="mortgage", rich_help_panel="Service")

app.add_typer(ade_app, name="ade", rich_help_panel="Database")

app.add_typer(app_tools_app, name="app", rich_help_panel="Checkout tools")
app.add_typer(batch_app, name="batch", rich_help_panel="Checkout tools")
app.add_typer(documents_app, name="documents", rich_help_panel="Local files")
app.add_typer(build_app, name="build", rich_help_panel="Checkout tools")
app.add_typer(audit_app, name="audit", rich_help_panel="Checkout tools")
app.add_typer(dev_app, name="dev", rich_help_panel="Checkout tools")


@app.callback()
def _root_options(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Emette i dati su stdout in formato JSON"),
):
    """Configure output shared by all command groups."""
    ctx.ensure_object(dict)
    enabled = json_output or os.getenv("SISTER_CLI_JSON") == "1"
    ctx.obj["json"] = enabled
    _runtime.configure_output(json_output=enabled)
    ctx.call_on_close(_runtime.reset_output)


console = _runtime.console


# -- helpers ------------------------------------------------------------------


def _registered_api_commands() -> list[tuple[str, str, str, str, bool]]:
    """Collect API metadata attached to the registered Typer callbacks."""
    from typer.main import get_command

    rows: list[tuple[str, str, str, str, bool]] = []

    def visit(group, path: tuple[str, ...] = ()) -> None:
        for name, command in sorted(getattr(group, "commands", {}).items()):
            command_path = (*path, name)
            callback = getattr(command, "callback", None)
            endpoint = getattr(callback, "endpoint", None)
            if endpoint and not getattr(command, "hidden", False) and not getattr(callback, "query_form", False):
                label = " ".join(command_path)
                description = command.help or next(iter((getattr(callback, "__doc__", "") or "").splitlines()), "")
                rows.append(
                    (
                        label,
                        getattr(callback, "method", "GET"),
                        endpoint,
                        description,
                        bool(getattr(callback, "paid", False)),
                    )
                )
            if hasattr(command, "commands"):
                visit(command, command_path)

    visit(get_command(app))
    return rows


@app.command("floor-plan-validate", hidden=True)
def floor_plan_validate(
    visura: Path = typer.Option(..., "--visura", help="File JSON con i dati della visura SISTER"),
    floor_plan: Path = typer.Option(..., "--floor-plan", help="PDF o immagine della planimetria"),
    calibration: Optional[Path] = typer.Option(None, "--calibration", help="File JSON di calibrazione"),
    rooms: Optional[Path] = typer.Option(None, "--rooms", help="File JSON con locali e poligoni"),
    property_index: int = typer.Option(0, "--property-index", min=0, help="Immobile in data.immobili da confrontare"),
    visura_area_m2: Optional[float] = typer.Option(None, "--visura-area-m2", min=0, help="Sostituisce la superficie estratta"),
    tolerance_m2: float = typer.Option(1.0, "--tolerance-m2", min=0, help="Tolleranza assoluta in metri quadrati"),
    tolerance_percent: float = typer.Option(0.05, "--tolerance-percent", min=0, help="Tolleranza relativa (0.05 = 5%%)"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Scrive il rapporto JSON"),
):
    """Alias deprecato per `sister documents floor-plan-validate`."""
    typer.echo("`sister floor-plan-validate` è deprecato; usa `sister documents floor-plan-validate`.", err=True)
    from .documents import documents_floor_plan_validate

    return documents_floor_plan_validate(
        visura=visura,
        floor_plan=floor_plan,
        calibration=calibration,
        rooms=rooms,
        property_index=property_index,
        visura_area_m2=visura_area_m2,
        tolerance_m2=tolerance_m2,
        tolerance_percent=tolerance_percent,
        output=output,
    )


# =============================================================================
# top-level commands
# =============================================================================


@app.command(rich_help_panel="Service")
def queries():
    """Elenca gli endpoint disponibili di sister."""
    table = Table(title="Endpoint dell'API visure", header_style="bold cyan")
    table.add_column("Comando", style="cyan", no_wrap=True)
    table.add_column("Metodo", style="dim", no_wrap=True)
    table.add_column("Endpoint", style="white")
    table.add_column("Descrizione")

    # one row per single-step query, straight from the spec that also generates the commands
    rows = [
        (f"query {form.command}", "POST", f"/api/v1{form.path}", form.summary, form.paid)
        for form in QUERY_FORMS.values()
    ]
    rows.extend(_registered_api_commands())
    for cmd, method, ep, desc, paid in rows:
        table.add_row(cmd, method, ep, f"[yellow]PAID[/yellow] {desc}" if paid else desc)

    client = VisuraClient()
    if _runtime.json_output_enabled():
        _runtime.emit_data(
            {
                "commands": [
                    {"command": cmd, "method": method, "endpoint": endpoint, "description": description, "paid": paid}
                    for cmd, method, endpoint, description, paid in rows
                ],
                "service_url": client.base_url,
            }
        )
    else:
        console.print(table)
        console.print(f"[dim]URL del servizio: {client.base_url}[/dim]")


@app.command(rich_help_panel="Service")
def health():
    """Verifica lo stato del servizio sister (GET /health)."""
    try:
        result = _runtime.run_api(lambda client: client.health())
    except typer.Exit:
        raise
    except Exception as e:
        base_url = os.getenv("VISURA_API_URL", "http://localhost:8025")
        console.print(f"[bold red]Errore:[/bold red] Impossibile raggiungere sister all'indirizzo {base_url}: {e}")
        raise typer.Exit(1) from None

    status = result.get("status", "unknown")
    authenticated = result.get("authenticated", False)
    console.print(render_health(result, os.getenv("VISURA_API_URL", "http://localhost:8025")))
    _runtime.emit_data(result)
    if status != "healthy" or not authenticated:
        raise typer.Exit(1)


health.endpoint = "/health"
health.method = "GET"


# -- entry point --------------------------------------------------------------


def _load_environment() -> None:
    """Read the service's .env (the shell's own variables win).

    `uv run --no-project sister ...` does not load it, so VISURA_API_URL / VISURA_API_KEY and the database settings
    would be missing. Done in the entry point, not at import, so tests importing this module keep their environment.
    """
    load_environment()
    if os.getenv("API_KEY"):
        os.environ.setdefault("VISURA_API_KEY", os.environ["API_KEY"])  # the service's key is the one the CLI must send


def run():
    """Entry point for the sister CLI."""
    _load_environment()
    app()


if __name__ == "__main__":
    run()
