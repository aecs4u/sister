"""CLI for the SISTER cadastral visura service.

Provides subcommands to submit cadastral searches, poll for results,
query history, and check service health.

Usage:
    sister query search -P Trieste -C TRIESTE -F 9 -p 166
    sister query intestati -P Trieste -C TRIESTE -F 9 -p 166 -t F -sub 3
    sister query workflow -P Trieste -C TRIESTE -F 9 -p 166 -t F
    sister query batch --input parcels.csv --wait
    sister query batch --input batch.json -O outputs/run1 [--resume]
    sister get <request_id>
    sister wait <request_id>
    sister requests --status pending
    sister history --provincia Trieste --limit 20
    sister health
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

import typer
from dotenv import find_dotenv, load_dotenv
from rich.table import Table

from ..client import VisuraClient
from ..query_forms import QUERY_FORMS
from . import _runtime, tools
from . import query as _query
from .ade import ade_app
from .db import db_app
from .documents import documents_app
from .mortgage import mortgage_app
from .requests import register_legacy_request_commands
from .workflow import workflow_app

query_app = _query.query_app
_interactive = _query._interactive
_parse_stop_at = _query._parse_stop_at

subprocess = tools.subprocess  # compatibility export; tests and callers should patch sister.cli.tools directly


app = typer.Typer(
    name="sister",
    help="CLI del servizio di visure catastali SISTER",
    no_args_is_help=True,
)
app.add_typer(query_app, name="query")

app.add_typer(db_app, name="db")

app.add_typer(workflow_app, name="workflow")

app.add_typer(mortgage_app, name="mortgage")

app.add_typer(ade_app, name="ade")

app_tools_app = tools.app_tools_app
batch_app = tools.batch_app
build_app = tools.build_app
audit_app = tools.audit_app
dev_app = tools.dev_app

app.add_typer(app_tools_app, name="app")
app.add_typer(batch_app, name="batch")
app.add_typer(documents_app, name="documents")
app.add_typer(build_app, name="build")
app.add_typer(audit_app, name="audit")
app.add_typer(dev_app, name="dev")


def _project_root() -> Path:
    """The checkout that holds ``scripts/``, ``.env`` and ``outputs/``.

    An editable install is the checkout itself; a regular install (``site-packages``) has none of them, so the tools
    run from ``$SISTER_HOME`` or from the current directory when that is a checkout.
    """
    try:
        return _runtime.project_root()
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc


_tool_commands = tools._tool_commands


console = _runtime.console


# -- helpers ------------------------------------------------------------------


_handle_api_error = _runtime.handle_api_error
_write_output = _runtime.write_output


@app.command("floor-plan-validate")
def floor_plan_validate(
    visura: Path = typer.Option(..., "--visura", help="File JSON con la risposta/i dati della visura SISTER"),
    floor_plan: Path = typer.Option(..., "--floor-plan", help="PDF o immagine della planimetria"),
    calibration: Optional[Path] = typer.Option(None, "--calibration", help="File JSON di calibrazione"),
    rooms: Optional[Path] = typer.Option(None, "--rooms", help="File JSON con locali/poligoni"),
    property_index: int = typer.Option(0, "--property-index", min=0, help="Immobile in data.immobili da confrontare"),
    visura_area_m2: Optional[float] = typer.Option(
        None, "--visura-area-m2", min=0, help="Sostituisce la superficie catastale estratta dal JSON"
    ),
    tolerance_m2: float = typer.Option(1.0, "--tolerance-m2", min=0, help="Tolleranza assoluta in metri quadrati"),
    tolerance_percent: float = typer.Option(
        0.05, "--tolerance-percent", min=0, help="Tolleranza relativa (0.05 = 5%%)"
    ),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Scrive il risultato della validazione in JSON"),
):
    """Confronta la superficie catastale di una visura con la stima della planimetria di Ocular."""
    try:
        from ..floor_plan import FloorPlanFeatureUnavailable, validate_visura_against_floor_plan

        visura_data = json.loads(visura.read_text(encoding="utf-8"))
        calibration_data = json.loads(calibration.read_text(encoding="utf-8")) if calibration else None
        rooms_data = json.loads(rooms.read_text(encoding="utf-8")) if rooms else None
        result = asyncio.run(
            validate_visura_against_floor_plan(
                visura=visura_data,
                floor_plan_path=floor_plan,
                calibration=calibration_data,
                rooms=rooms_data,
                property_index=property_index,
                visura_area_m2=visura_area_m2,
                tolerance_m2=tolerance_m2,
                tolerance_percent=tolerance_percent,
            )
        )
    except FloorPlanFeatureUnavailable as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        console.print(f"[red]Invalid floor-plan validation input: {exc}[/red]")
        raise typer.Exit(1)
    except Exception as exc:
        console.print(f"[red]Floor-plan validation failed: {exc}[/red]")
        raise typer.Exit(1)

    comparison = result["comparison"]
    status = comparison["status"]
    style = {"match": "green", "mismatch": "yellow", "unavailable": "red"}.get(status, "red")
    console.print(f"[{style}]Validation: {status}[/{style}]")
    console.print(f"  Visura:     {comparison.get('visura_area_m2') or 'n/a'} m²")
    console.print(f"  Planimetry: {comparison.get('estimated_area_m2') or 'n/a'} m²")
    if comparison.get("difference_m2") is not None:
        console.print(f"  Difference:  {comparison['difference_m2']} m²")
    if output:
        _write_output(result, str(output))




# =============================================================================
# top-level commands
# =============================================================================


@app.command()
def queries():
    """Elenca gli endpoint disponibili di sister."""
    table = Table(title="Visura API endpoints", header_style="bold cyan")
    table.add_column("Command", style="cyan", no_wrap=True)
    table.add_column("Method", style="dim", no_wrap=True)
    table.add_column("Endpoint", style="white")
    table.add_column("Description")

    # one row per single-step query, straight from the spec that also generates the commands
    rows = [(f"query {form.command}", "POST", f"/api/v1{form.path}", form.summary) for form in QUERY_FORMS.values()]
    rows += [
        ("query workflow", "POST", "/api/v1/catasto/workflow/run", "Multi-step workflow (--preset), or search → intestati"),
        ("workflow status", "GET", "/api/v1/catasto/workflow/runs/{id}", "Status of a workflow"),
        ("workflow cancel", "POST", "/api/v1/catasto/workflow/runs/{id}/cancel", "Cancel a workflow"),
        ("query ipotecaria-*", "POST", "/api/v1/visura/ispezione-ipotecaria", "Ispezione ipotecaria (PAID)"),
        ("query batch", "POST", "/api/v1/visura (×N)", "Batch of queries from a JSON/Markdown/CSV file (resumable)"),
        ("get", "GET", "/api/v1/visura/{request_id}", "Poll for a single result"),
        ("wait", "GET", "/api/v1/visura/{request_id}", "Poll until complete or timeout"),
        ("requests", "GET", "/api/v1/visura/history", "List all requests with status"),
        ("history", "GET", "/api/v1/visura/history", "Query response history"),
        ("health", "GET", "/health", "Service health check"),
    ]
    for cmd, method, ep, desc in rows:
        table.add_row(cmd, method, ep, desc)

    console.print(table)

    client = VisuraClient()
    console.print(f"[dim]Service URL: {client.base_url}[/dim]")


register_legacy_request_commands(app)



@app.command()
def health():
    """Verifica lo stato del servizio sister (GET /health)."""
    try:
        result = _runtime.run_api(lambda client: client.health())
    except typer.Exit:
        raise
    except Exception as e:
        base_url = os.getenv("VISURA_API_URL", "http://localhost:8025")
        console.print(f"[bold red]Error:[/bold red] Cannot reach sister at {base_url}: {e}")
        raise typer.Exit(1) from None

    status = result.get("status", "unknown")
    authenticated = result.get("authenticated", False)
    queue_size = result.get("queue_size", "?")
    cached = result.get("cached_responses", "?")
    pending = result.get("pending_requests", "?")
    db_stats = result.get("database", {})

    status_style = "green" if status == "healthy" else "red"
    auth_style = "green" if authenticated else "red"

    table = Table(title="Visura API Health", header_style="bold cyan")
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value", style="white")

    table.add_row("Status", f"[{status_style}]{status}[/{status_style}]")
    table.add_row("Authenticated", f"[{auth_style}]{authenticated}[/{auth_style}]")
    table.add_row("Queue size", str(queue_size))
    table.add_row("Pending requests", str(pending))
    table.add_row("Cached responses", str(cached))
    table.add_row("Queue max size", str(result.get("queue_max_size", "?")))
    table.add_row("Response TTL", f"{result.get('response_ttl_seconds', '?')}s")

    if db_stats:
        table.add_section()
        table.add_row("DB total requests", str(db_stats.get("total_requests", "?")))
        table.add_row("DB total responses", str(db_stats.get("total_responses", "?")))
        table.add_row("DB successful", str(db_stats.get("successful", "?")))
        table.add_row("DB failed", str(db_stats.get("failed", "?")))

    console.print(table)
    console.print(f"[dim]Service URL: {os.getenv('VISURA_API_URL', 'http://localhost:8025')}[/dim]")
    if status != "healthy" or not authenticated:
        raise typer.Exit(1)


# -- entry point --------------------------------------------------------------


def _load_environment() -> None:
    """Read the service's .env (the shell's own variables win).

    `uv run --no-project sister ...` does not load it, so VISURA_API_URL / VISURA_API_KEY and the database settings
    would be missing. Done in the entry point, not at import, so tests importing this module keep their environment.
    """
    load_dotenv(find_dotenv(usecwd=True))
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    if os.getenv("API_KEY"):
        os.environ.setdefault("VISURA_API_KEY", os.environ["API_KEY"])  # the service's key is the one the CLI must send


def run():
    """Entry point for the sister CLI."""
    _load_environment()
    app()


if __name__ == "__main__":
    run()
