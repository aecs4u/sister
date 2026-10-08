"""CLI for the SISTER cadastral visura service.

Provides subcommands to submit cadastral searches, poll for results,
query history, and check service health.

Usage:
    sister query search -P Trieste -C TRIESTE -F 9 -p 166
    sister query intestati -P Trieste -C TRIESTE -F 9 -p 166 -t F -sub 3
    sister query workflow -P Trieste -C TRIESTE -F 9 -p 166 -t F
    sister query batch --input parcels.csv --wait
    sister get <request_id>
    sister wait <request_id>
    sister requests --status pending
    sister history --provincia Trieste --limit 20
    sister health
"""

import asyncio
import csv
import inspect
import io
import json
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .client import VisuraAPIError, VisuraClient
from .query_forms import QUERY_FORMS, get_query_form, option_flags, param_names, validate_params

app = typer.Typer(
    name="sister",
    help="SISTER cadastral visura service CLI",
    no_args_is_help=True,
)
query_app = typer.Typer(
    name="query",
    help="Submit cadastral queries (search, intestati, workflow, batch)",
    no_args_is_help=True,
)
app.add_typer(query_app, name="query")

db_app = typer.Typer(
    name="db",
    help="Database management (init, migrate, status)",
    no_args_is_help=True,
)
app.add_typer(db_app, name="db")

console = Console()


# -- helpers ------------------------------------------------------------------


def _handle_api_error(e: VisuraAPIError) -> None:
    """Print a styled error from the sister and exit."""
    console.print(f"[bold red]Error:[/bold red] sister returned HTTP {e.status_code}: {e.detail}")
    raise typer.Exit(1)


def _write_output(data: dict | list, path: str) -> None:
    """Write data to a file, auto-detecting format from the extension."""
    p = Path(path)
    content = json.dumps(data, indent=2, ensure_ascii=False)
    p.write_text(content, encoding="utf-8")
    console.print(f"[dim]Output written to {p}[/dim]")


@app.command("floor-plan-validate")
def floor_plan_validate(
    visura: Path = typer.Option(..., "--visura", help="JSON file containing the SISTER visura response/data"),
    floor_plan: Path = typer.Option(..., "--floor-plan", help="Floor-plan PDF or image"),
    calibration: Optional[Path] = typer.Option(None, "--calibration", help="JSON calibration file"),
    rooms: Optional[Path] = typer.Option(None, "--rooms", help="JSON file containing rooms/polygons"),
    property_index: int = typer.Option(0, "--property-index", min=0, help="Property in data.immobili to compare"),
    visura_area_m2: Optional[float] = typer.Option(
        None, "--visura-area-m2", min=0, help="Override the cadastral surface extracted from the JSON"
    ),
    tolerance_m2: float = typer.Option(1.0, "--tolerance-m2", min=0, help="Absolute tolerance in square metres"),
    tolerance_percent: float = typer.Option(
        0.05, "--tolerance-percent", min=0, help="Relative tolerance (0.05 = 5%%)"
    ),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write the validation result to JSON"),
):
    """Validate a cadastral visura surface against Ocular's floor-plan estimate."""
    try:
        from .floor_plan import FloorPlanFeatureUnavailable, validate_visura_against_floor_plan

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


def _print_result(result: dict) -> None:
    """Pretty-print a visura result with status-aware formatting."""
    status = result.get("status", "unknown")
    request_id = result.get("request_id", "?")

    if status == "processing":
        console.print(
            f"[yellow]Request {request_id} is still processing.[/yellow]\n"
            f"[dim]Run again later or use: sister wait {request_id}[/dim]"
        )
        return

    if status == "expired":
        console.print(f"[red]Request {request_id} has expired (cache evicted).[/red]")
        return

    if status == "error":
        error = result.get("error", "unknown error")
        console.print(f"[red]Request {request_id} failed:[/red] {error}")
        return

    if status == "needs_human":
        console.print(
            f"[yellow]Request {request_id} needs a human:[/yellow] the SISTER CAPTCHA was not solved, "
            "so no document was requested."
        )
        return

    if status == "completed":
        tipo = result.get("tipo_catasto", "")
        data = result.get("data", {})
        timestamp = result.get("timestamp", "")

        console.print(
            f"[bold green]Completed[/bold green] {request_id}"
            + (f"  [dim]({tipo})[/dim]" if tipo else "")
            + (f"  [dim]{timestamp}[/dim]" if timestamp else "")
        )

        # Display immobili table if present
        immobili = data.get("immobili", []) if isinstance(data, dict) else []
        if immobili:
            table = Table(title=f"Immobili ({len(immobili)})", header_style="bold cyan")
            cols = list(immobili[0].keys())
            for col in cols:
                table.add_column(col, no_wrap=(col in ("Foglio", "Particella", "Sub")))
            for row in immobili:
                table.add_row(*[str(row.get(c, "")) for c in cols])
            console.print(table)

        # Display intestati table if present
        intestati = data.get("intestati", []) if isinstance(data, dict) else []
        if intestati:
            table = Table(title=f"Intestati ({len(intestati)})", header_style="bold cyan")
            cols = list(intestati[0].keys())
            for col in cols:
                table.add_column(col)
            for row in intestati:
                table.add_row(*[str(row.get(c, "")) for c in cols])
            console.print(table)

        # Fall back to JSON if no structured tables
        if not immobili and not intestati and data:
            console.print(json.dumps(data, indent=2, ensure_ascii=False))

        return

    # Unknown status — dump full result
    console.print(json.dumps(result, indent=2, ensure_ascii=False))


# =============================================================================
# query subcommands
# =============================================================================


# ---------------------------------------------------------------------------
# Single-step queries: every ``sister query <command>`` is generated from sister.query_forms
# ---------------------------------------------------------------------------


def _report_submission(client: VisuraClient, params: dict, result: dict, wait: bool, output: Optional[str]) -> None:
    """Print what was submitted and, with --wait, poll every request until it is done."""
    request_ids = client.request_ids(result)
    console.print(f"[bold green]Request submitted[/bold green] (status: {result.get('status', 'unknown')})")
    for rid in request_ids:
        console.print(f"  ID: [cyan]{rid}[/cyan]")
    if params.get("codice_fiscale"):
        console.print(f"  CF: {params['codice_fiscale'].upper()}  Scope: {result.get('provincia', 'NAZIONALE')}")

    if not wait:
        console.print(
            "[dim]Poll results with:[/dim]\n" + "\n".join(f"  [bold]sister get {rid}[/bold]" for rid in request_ids)
        )
        console.print(
            "[dim]Or wait automatically:[/dim]\n"
            + "\n".join(f"  [bold]sister wait {rid}[/bold]" for rid in request_ids)
        )
        if output:
            _write_output(result, output)
        return

    all_results = {}
    for rid in request_ids:
        console.print(f"\n[dim]Waiting for {rid}...[/dim]")
        try:
            res = asyncio.run(client.wait_for_result(rid))
            all_results[rid] = res
            _print_result(res)
        except TimeoutError as e:
            console.print(f"[yellow]{e}[/yellow]")
        except VisuraAPIError as e:
            if len(request_ids) == 1:
                _handle_api_error(e)
            console.print(f"[red]{rid}: HTTP {e.status_code}: {e.detail}[/red]")
    if output and all_results:
        _write_output(all_results if len(all_results) > 1 else next(iter(all_results.values())), output)


def _run_query_command(command: str, params: dict, wait: bool, output: Optional[str], dry_run: bool, force: bool) -> None:
    """Shared body of every generated query command: validate → (dry run) → submit → report."""
    spec = QUERY_FORMS[command]
    try:
        validate_params(command, params)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(2)

    client = VisuraClient()
    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] — request will not be sent")
        console.print(f"  POST {client.base_url}{spec.path}")
        console.print(f"  Body: {json.dumps(params, ensure_ascii=False)}")
        return
    try:
        result = asyncio.run(client.submit(command, params, force=force))
    except VisuraAPIError as e:
        _handle_api_error(e)
        return
    _report_submission(client, params, result, wait, output)


def _make_query_command(form):
    """Build the Typer command of a query: one option per parameter of its spec plus the common ones."""
    parameters: list[inspect.Parameter] = []
    seen: set[str] = set()
    for fld in form.fields:
        if fld.param in seen:
            continue
        seen.add(fld.param)
        flags = option_flags(fld.param)
        if fld.kind == "checkbox":
            option, annotation = typer.Option(None, f"{flags[0]}/--no-{flags[0][2:]}", help=fld.help), Optional[bool]
        elif fld.param in form.required:
            option, annotation = typer.Option(..., *flags, help=fld.help), str
        else:
            option, annotation = typer.Option(None, *flags, help=fld.help), Optional[str]
        parameters.append(inspect.Parameter(fld.param, inspect.Parameter.KEYWORD_ONLY, default=option, annotation=annotation))
    common = {
        "output": (Optional[str], typer.Option(None, "--output", "-o", help="Output file path (.json)")),
        "wait": (bool, typer.Option(False, "--wait", "-w", help="Wait for the result instead of returning immediately")),
        "dry_run": (bool, typer.Option(False, "--dry-run", help="Preview the request without sending it")),
        "force": (bool, typer.Option(False, "--force", help="Bypass the cache, always submit a new request")),
    }
    for name, (annotation, option) in common.items():
        parameters.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=option, annotation=annotation))

    def command(**kwargs):
        output, wait, dry_run, force = (kwargs.pop(name) for name in common)
        params = {
            name: (("true" if value else "false") if isinstance(value, bool) else str(value))
            for name, value in kwargs.items()
            if value is not None
        }
        _run_query_command(form.command, params, wait, output, dry_run, force)

    command.__signature__ = inspect.Signature(parameters)
    command.__annotations__ = {p.name: p.annotation for p in parameters}
    command.__name__ = form.command.replace("-", "_")
    command.__doc__ = form.summary
    return command


for _form in QUERY_FORMS.values():
    query_app.command(_form.command)(_make_query_command(_form))


# -- Ispezioni Ipotecarie (paid service) --------------------------------------


def _ipotecaria_command(
    tipo_ricerca: str,
    provincia: str,
    client: VisuraClient,
    wait: bool,
    output: Optional[str],
    yes: bool = False,
    comune: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    codice_fiscale: Optional[str] = None,
    identificativo: Optional[str] = None,
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    numero_nota: Optional[str] = None,
    anno_nota: Optional[str] = None,
):
    """Shared logic for ispezione ipotecaria CLI commands."""
    if not yes:
        console.print(
            "[bold yellow]WARNING:[/bold yellow] Ispezioni Ipotecarie is a paid service. "
            "Each query may incur a cost.\n"
            "Use [bold]--yes[/bold] to auto-confirm cost."
        )

    try:
        result = asyncio.run(
            client.ispezione_ipotecaria(
                tipo_ricerca=tipo_ricerca,
                provincia=provincia,
                comune=comune,
                tipo_catasto=tipo_catasto,
                codice_fiscale=codice_fiscale,
                identificativo=identificativo,
                foglio=foglio,
                particella=particella,
                numero_nota=numero_nota,
                anno_nota=anno_nota,
                auto_confirm=yes,
            )
        )
    except VisuraAPIError as e:
        _handle_api_error(e)
        return

    request_id = result.get("request_id", "")
    console.print(f"[bold green]Request submitted[/bold green] (ispezione ipotecaria — {tipo_ricerca})")
    console.print(f"  ID: [cyan]{request_id}[/cyan]")

    if not wait:
        console.print(f"[dim]Poll result with:[/dim]\n  [bold]sister get {request_id}[/bold]")
        if output:
            _write_output(result, output)
        return

    console.print(f"\n[dim]Waiting for {request_id}...[/dim]")
    try:
        res = asyncio.run(client.wait_for_result(request_id))
        # Show cost info if present
        data = res.get("data", {})
        if isinstance(data, dict) and data.get("cost"):
            cost = data["cost"]
            console.print(f"[yellow]Cost: {cost.get('text', 'N/A')} (€{cost.get('value', 0):.2f})[/yellow]")
            if not data.get("confirmed"):
                console.print("[red]Cost not confirmed. Use --yes to auto-confirm.[/red]")
        _print_result(res)
        if output:
            _write_output(res, output)
    except TimeoutError as e:
        console.print(f"[yellow]{e}[/yellow]")
    except VisuraAPIError as e:
        _handle_api_error(e)


@query_app.command("ipotecaria-immobile")
def ipotecaria_immobile(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name"),
    comune: Optional[str] = typer.Option(None, "--comune", "-C", help="Municipality name"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Sheet number"),
    particella: Optional[str] = typer.Option(None, "--particella", "-p", help="Parcel number"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="'T' = Terreni, 'F' = Fabbricati"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
    force: bool = typer.Option(False, "--force", help="Bypass cache"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-confirm cost without prompting"),
):
    """Ispezione Ipotecaria by property (immobile). PAID SERVICE."""
    client = VisuraClient()
    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] POST /visura/ispezione-ipotecaria (immobile)")
        return
    _ipotecaria_command(
        "immobile",
        provincia,
        client,
        wait,
        output,
        yes=yes,
        comune=comune,
        tipo_catasto=tipo_catasto,
        foglio=foglio,
        particella=particella,
    )


@query_app.command("ipotecaria-persona")
def ipotecaria_persona(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name"),
    codice_fiscale: str = typer.Option(..., "--cf", help="Codice fiscale"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
    force: bool = typer.Option(False, "--force", help="Bypass cache"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-confirm cost without prompting"),
):
    """Ispezione Ipotecaria by person (codice fiscale). PAID SERVICE."""
    client = VisuraClient()
    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] POST /visura/ispezione-ipotecaria (persona_fisica)")
        return
    _ipotecaria_command(
        "persona_fisica",
        provincia,
        client,
        wait,
        output,
        yes=yes,
        codice_fiscale=codice_fiscale,
    )


@query_app.command("ipotecaria-azienda")
def ipotecaria_azienda(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name"),
    identificativo: str = typer.Option(..., "--id", help="P.IVA or company name"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
    force: bool = typer.Option(False, "--force", help="Bypass cache"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-confirm cost without prompting"),
):
    """Ispezione Ipotecaria by company (P.IVA or name). PAID SERVICE."""
    client = VisuraClient()
    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] POST /visura/ispezione-ipotecaria (persona_giuridica)")
        return
    _ipotecaria_command(
        "persona_giuridica",
        provincia,
        client,
        wait,
        output,
        yes=yes,
        identificativo=identificativo,
    )


@query_app.command("ipotecaria-nota")
def ipotecaria_nota(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name"),
    numero_nota: str = typer.Option(..., "--numero", "-n", help="Note number"),
    anno_nota: Optional[str] = typer.Option(None, "--anno", help="Note year"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
    force: bool = typer.Option(False, "--force", help="Bypass cache"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-confirm cost without prompting"),
):
    """Ispezione Ipotecaria by note reference. PAID SERVICE."""
    client = VisuraClient()
    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] POST /visura/ispezione-ipotecaria (nota)")
        return
    _ipotecaria_command(
        "nota",
        provincia,
        client,
        wait,
        output,
        yes=yes,
        numero_nota=numero_nota,
        anno_nota=anno_nota,
    )


# -- Workflow presets ---------------------------------------------------------

def _run_step(client, label, coro):
    """Run a single workflow step: submit → wait → print."""
    console.print(f"\n  [dim]{label}...[/dim]")
    try:
        submit = asyncio.run(coro)
        rid = submit.get("request_id", "")
        rids = submit.get("request_ids", [rid] if rid else [])
        results = _wait_for_requests(client, rids)
        return results[0] if len(results) == 1 else results
    except (TimeoutError, VisuraAPIError) as e:
        console.print(f"  [red]{e}[/red]")
        return {"status": "error", "error": str(e)}


def _wait_for_requests(client, request_ids, *, show_ids=True):
    """Wait for every submitted request, retaining per-request failures."""
    if show_ids:
        for request_id in request_ids:
            console.print(f"  ID: [cyan]{request_id}[/cyan]")

    results = []
    for request_id in request_ids:
        console.print(f"  [dim]Waiting for {request_id}...[/dim]")
        try:
            result = asyncio.run(client.wait_for_result(request_id))
            _print_result(result)
        except (TimeoutError, VisuraAPIError) as exc:
            console.print(f"  [red]{exc}[/red]")
            result = {"request_id": request_id, "status": "error", "error": str(exc)}
        results.append(result)
    return results


@query_app.command()
def workflow(
    preset: Optional[str] = typer.Option(
        None,
        "--preset",
        help="Workflow family: due-diligence, portfolio, fondiario, indirizzo, cross-reference",
    ),
    provincia: Optional[str] = typer.Option(None, "--provincia", "-P", help="Province name"),
    comune: Optional[str] = typer.Option(None, "--comune", "-C", help="Municipality name"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Sheet number"),
    particella: Optional[str] = typer.Option(None, "--particella", "-p", help="Parcel number"),
    tipo_catasto: Optional[str] = typer.Option(
        None, "--tipo-catasto", "-t", help="'T' = Terreni, 'F' = Fabbricati (omit for both)"
    ),
    sezione: Optional[str] = typer.Option(None, "--sezione", help="Section (optional)"),
    subalterno: Optional[str] = typer.Option(None, "--subalterno", "-sub", help="Limit intestati to this sub-unit"),
    codice_fiscale: Optional[str] = typer.Option(None, "--cf", help="Codice fiscale for soggetto search"),
    azienda_id: Optional[str] = typer.Option(None, "--azienda", help="P.IVA or company name"),
    indirizzo_str: Optional[str] = typer.Option(None, "--indirizzo", "-a", help="Street address"),
    numero_nota: Optional[str] = typer.Option(None, "--nota", help="Note/annotation number"),
    with_elenco: bool = typer.Option(False, "--elenco", help="List all properties in the comune"),
    with_mappa: bool = typer.Option(False, "--mappa", help="Fetch cadastral map data"),
    with_ispezioni: bool = typer.Option(False, "--ispezioni", help="Fetch inspection records"),
    with_fiduciali: bool = typer.Option(False, "--fiduciali", help="Fetch survey reference points"),
    with_originali: bool = typer.Option(False, "--originali", help="Fetch original registration records"),
    with_nota: bool = typer.Option(False, "--with-nota", help="Fetch annotation/note data"),
    with_ispezioni_cart: bool = typer.Option(False, "--ispezioni-cart", help="Fetch paper inspection records"),
    depth: str = typer.Option("standard", "--depth", "-d", help="Workflow depth: light, standard, deep, full"),
    max_fanout: int = typer.Option(20, "--max-fanout", help="Max properties/owners to fan out to per step"),
    max_owners: int = typer.Option(10, "--max-owners", help="Max owners to expand in owner_expand"),
    max_properties_per_owner: int = typer.Option(
        20, "--max-properties-per-owner", help="Max properties per owner in portfolio drill"
    ),
    max_historical_properties: int = typer.Option(5, "--max-history", help="Max properties to run history bundle on"),
    max_paid_steps: int = typer.Option(3, "--max-paid", help="Max paid step invocations (ispezione ipotecaria)"),
    max_total_steps: int = typer.Option(100, "--max-steps", help="Overall circuit breaker for total step executions"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-confirm paid service costs"),
    include_paid: bool = typer.Option(False, "--include-paid", help="Include paid steps (e.g. ispezione ipotecaria)"),
    include_history: bool = typer.Option(False, "--history", help="Include historical note and paper-inspection queries"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path (.json)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview steps without executing"),
    force: bool = typer.Option(False, "--force", help="Bypass cache, always submit new request"),
):
    """Multi-phase workflow with optional preset.

    \b
    Without --preset: runs search → intestati plus any flags you enable.
    With --preset: runs a named sequence of steps automatically.

    \b
    Workflow families:
      due-diligence        parcel checks; add --history for notes and paper inspections
      portfolio            person (--cf) or company (--azienda) asset investigation
      fondiario            land survey and cadastral-map records
      indirizzo            address → property search
      cross-reference      compare a person's and a company's properties

    \b
    Depth modes:
      light     Only core discovery steps (fast, free)
      standard  Adds per-property enrichment (default)
      deep      Adds owner expansion, paid inspections (requires --include-paid --yes)
      full      Multi-hop graph expansion with budgets (requires --include-paid --yes for paid steps)

    \b
    Examples:
      uv run sister query workflow --preset due-diligence -P Trieste -C TRIESTE -F 9 -p 166
      uv run sister query workflow --preset due-diligence -P Roma -C ROMA -F 1 -p 1 --history --nota 5678
      uv run sister query workflow --preset portfolio --cf RSSMRA85M01H501Z
      uv run sister query workflow --preset portfolio --azienda 02471840997 --depth full
      uv run sister query workflow --preset fondiario -P Roma -C ROMA -F 100
      uv run sister query workflow --preset due-diligence -P Roma -C ROMA -F 1 -p 1 --depth deep --include-paid --yes
      uv run sister query workflow --preset due-diligence -P Roma -C ROMA -F 1 -p 1 --depth full --include-paid --yes --max-paid 5
      uv run sister query workflow -P Trieste -C TRIESTE -F 9 -p 166 --elenco --mappa
    """
    # -- Server-side preset execution ------------------------------------------

    if preset:
        from .workflows import prepare_workflow

        try:
            plan = prepare_workflow(
                {
                    "preset": preset,
                    "provincia": provincia,
                    "comune": comune,
                    "foglio": foglio,
                    "particella": particella,
                    "tipo_catasto": tipo_catasto,
                    "sezione": sezione,
                    "sezione_urbana": None,
                    "subalterno": subalterno,
                    "codice_fiscale": codice_fiscale,
                    "identificativo": azienda_id,
                    "indirizzo": indirizzo_str,
                    "numero_nota": numero_nota,
                    "include_history": include_history,
                    "depth": depth,
                    "include_paid_steps": include_paid,
                    "auto_confirm": yes,
                    "max_fanout": max_fanout,
                    "max_owners": max_owners,
                    "max_properties_per_owner": max_properties_per_owner,
                    "max_historical_properties": max_historical_properties,
                    "max_paid_steps": max_paid_steps,
                    "max_total_steps": max_total_steps,
                }
            )
        except (ValueError, KeyError) as exc:
            console.print(f"[red]Invalid workflow: {exc}[/red]")
            raise typer.Exit(1) from exc

        if dry_run:
            console.print(
                f"[bold yellow]DRY RUN[/bold yellow] — POST /visura/workflow (preset={preset}, depth={depth})"
            )
            console.print(f"  {plan.description}")
            console.print(f"  Steps: {' → '.join(plan.steps)}")
            console.print(
                f"  Depth: [cyan]{depth}[/cyan]  Fanout: [cyan]{max_fanout}[/cyan]  Owners: [cyan]{max_owners}[/cyan]"
            )
            console.print(
                f"  Props/owner: [cyan]{max_properties_per_owner}[/cyan]  History: [cyan]{max_historical_properties}[/cyan]  Paid: [cyan]{max_paid_steps}[/cyan]  Max steps: [cyan]{max_total_steps}[/cyan]"
            )
            if include_paid:
                console.print(f"  [yellow]Paid steps enabled (auto_confirm={yes})[/yellow]")
            return

        console.print(f"[bold]Preset: {preset}[/bold] — {plan.description}")
        console.print(f"[dim]Depth: {depth} | Max fanout: {max_fanout}[/dim]")

        client = VisuraClient()
        try:
            result = asyncio.run(
                client.workflow(
                    preset=preset,
                    provincia=provincia,
                    comune=comune,
                    foglio=foglio,
                    particella=particella,
                    tipo_catasto=tipo_catasto,
                    sezione=sezione,
                    subalterno=subalterno,
                    codice_fiscale=codice_fiscale,
                    identificativo=azienda_id,
                    indirizzo=indirizzo_str,
                    numero_nota=numero_nota,
                    include_history=include_history,
                    depth=depth,
                    max_fanout=max_fanout,
                    max_owners=max_owners,
                    max_properties_per_owner=max_properties_per_owner,
                    max_historical_properties=max_historical_properties,
                    max_paid_steps=max_paid_steps,
                    max_total_steps=max_total_steps,
                    auto_confirm=yes,
                    include_paid_steps=include_paid,
                )
            )
        except VisuraAPIError as e:
            _handle_api_error(e)
            return

        if result.get("error"):
            console.print(f"[red]Error: {result['error']}[/red]")
            raise typer.Exit(1)

        # Print step results
        steps_data = result.get("steps", [])
        for step in steps_data:
            status = step.get("status", "unknown")
            step_name = step.get("step", "?")
            style = "green" if status == "completed" else ("red" if status == "error" else "yellow")
            console.print(f"\n  [{style}]{step_name}[/{style}] — {status}")

            if status == "error":
                console.print(f"    [red]{step.get('error', '')}[/red]")
            elif status == "completed" and step.get("data"):
                data = step["data"]
                if isinstance(data, dict):
                    # Show counts for known keys
                    for key in ("immobili", "intestati", "risultati", "drill_results"):
                        items = data.get(key, [])
                        if items and isinstance(items, list):
                            console.print(f"    {key}: [cyan]{len(items)}[/cyan] records")
                    if "total" in data:
                        console.print(f"    total: [cyan]{data['total']}[/cyan]")
                    if data.get("truncated"):
                        console.print("    [yellow]Results truncated (max 20 drill-down properties)[/yellow]")

        # Summary
        summary = result.get("summary", {})
        console.rule("[bold green]Workflow complete[/bold green]")
        console.print(
            f"  Steps: [green]{summary.get('completed', 0)}[/green] completed, "
            f"[red]{summary.get('failed', 0)}[/red] failed, "
            f"[yellow]{summary.get('skipped', 0)}[/yellow] skipped"
        )
        if summary.get("properties", 0) > 0:
            console.print(
                f"  Properties: [cyan]{summary['properties']}[/cyan]  Owners: [cyan]{summary.get('owners', 0)}[/cyan]"
            )
        if summary.get("risk_flags", 0) > 0:
            console.print(f"  [yellow]Risk flags: {summary['risk_flags']}[/yellow]")

        if output:
            _write_output(result, output)
        return

    # -- Custom workflow (no preset) — client-side orchestration ---------------

    client = VisuraClient()
    all_data: dict = {}

    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] — custom workflow preview")
        if codice_fiscale:
            console.print(f"  → soggetto CF={codice_fiscale}")
        if azienda_id:
            console.print(f"  → azienda ID={azienda_id}")
        if indirizzo_str:
            console.print(f"  → indirizzo '{indirizzo_str}' {provincia}/{comune}")
        if foglio and particella:
            console.print(f"  → search {provincia}/{comune} F.{foglio} P.{particella}")
            console.print("  → intestati (for each sub-unit)")
        if with_elenco:
            console.print(f"  → elenco immobili {provincia}/{comune}")
        if with_mappa:
            console.print(f"  → mappa F.{foglio}")
        if with_ispezioni:
            console.print("  → ispezioni")
        if with_fiduciali:
            console.print("  → fiduciali")
        if with_originali:
            console.print("  → originali")
        if with_nota:
            console.print("  → nota")
        if with_ispezioni_cart:
            console.print("  → ispezioni cartacee")
        return

    # -- Phase: soggetto / azienda (if starting from person/company) ----------

    is_person_start = bool(codice_fiscale and not foglio)
    is_company_start = bool(azienda_id and not foglio and not codice_fiscale)

    if codice_fiscale and is_person_start:
        console.rule("[bold cyan]Soggetto — National CF search[/bold cyan]")
        all_data["soggetto"] = _run_step(
            client,
            f"Soggetto CF={codice_fiscale}",
            client.soggetto(codice_fiscale=codice_fiscale, tipo_catasto=tipo_catasto, provincia=provincia),
        )

    if azienda_id and is_company_start:
        console.rule("[bold cyan]Azienda — Company search[/bold cyan]")
        all_data["persona_giuridica"] = _run_step(
            client,
            f"Persona giuridica ID={azienda_id}",
            client.persona_giuridica(identificativo=azienda_id, tipo_catasto=tipo_catasto, provincia=provincia),
        )

    # For patrimonio/aziendale: if we got properties from soggetto/azienda,
    # we could drill into each one — but that requires parsing the result table
    # and submitting per-property requests. For now, the soggetto/azienda result
    # already contains the property list. Intestati drill-down from soggetto
    # results would need the user to select specific properties.

    # -- Phase: indirizzo lookup (if starting from address) -------------------

    if indirizzo_str and provincia and comune:
        console.rule("[bold cyan]Indirizzo — Address lookup[/bold cyan]")
        all_data["indirizzo"] = _run_step(
            client,
            f"Indirizzo '{indirizzo_str}' in {provincia}/{comune}",
            client.generic_search(
                search_type="indirizzo",
                provincia=provincia,
                comune=comune,
                tipo_catasto=tipo_catasto or "T",
                indirizzo=indirizzo_str,
            ),
        )

    # -- Phase: search immobili (if we have foglio/particella) ----------------

    all_immobili = []
    search_results = {}

    if foglio and particella and provincia and comune:
        console.rule("[bold cyan]Search — Immobili[/bold cyan]")
        try:
            search_result = asyncio.run(
                client.search(
                    provincia=provincia,
                    comune=comune,
                    foglio=foglio,
                    particella=particella,
                    tipo_catasto=tipo_catasto,
                    sezione=sezione,
                )
            )
            request_ids = search_result.get("request_ids", [])
            console.print(f"Submitted {len(request_ids)} request(s)")
            search_results = dict(
                zip(request_ids, _wait_for_requests(client, request_ids, show_ids=False))
            )
        except VisuraAPIError as e:
            console.print(f"[red]Search failed: {e}[/red]")

        for res in search_results.values():
            if res.get("status") == "completed":
                data = res.get("data", {})
                if isinstance(data, dict):
                    all_immobili.extend(data.get("immobili", []))
        all_data["immobili"] = all_immobili

    # -- Phase: intestati (if search found immobili) --------------------------

    intestati_results = []

    if all_immobili and provincia and comune and foglio and particella:
        intestati_targets = []
        for res in search_results.values():
            if res.get("status") != "completed":
                continue
            tc = res.get("tipo_catasto", "")
            data = res.get("data", {})
            immobili = data.get("immobili", []) if isinstance(data, dict) else []

            if tc == "T":
                if not subalterno:
                    intestati_targets.append(("T", None))
            elif tc == "F":
                subs_found = {imm.get("Sub", "").strip() for imm in immobili if imm.get("Sub", "").strip()}
                if subalterno:
                    intestati_targets.append(("F", subalterno))
                elif subs_found:
                    intestati_targets.extend(("F", sub) for sub in sorted(subs_found))

        if intestati_targets:
            console.rule("[bold cyan]Intestati — Ownership[/bold cyan]")
            for tc, sub in intestati_targets:
                sub_label = f" Sub.{sub}" if sub else ""
                response = _run_step(
                    client,
                    f"{tc}{sub_label}",
                    client.intestati(
                        provincia=provincia,
                        comune=comune,
                        foglio=foglio,
                        particella=particella,
                        tipo_catasto=tc,
                        subalterno=sub,
                        sezione=sezione,
                    ),
                )
                responses = response if isinstance(response, list) else [response]
                for res in responses:
                    intestati_results.append(
                        {
                            "tipo_catasto": tc,
                            "subalterno": sub,
                            "request_id": res.get("request_id", ""),
                            **res,
                        }
                    )

    all_data["intestati_results"] = intestati_results

    # -- Phase: enrichment (elenco, mappa, fiduciali, originali, etc.) --------

    enrichment_steps = []
    if with_elenco and provincia and comune:
        enrichment_steps.append(
            (
                "elenco_immobili",
                f"Elenco immobili {provincia}/{comune}",
                client.elenco_immobili(provincia=provincia, comune=comune, tipo_catasto=tipo_catasto, foglio=foglio),
            )
        )
    if with_mappa and provincia and comune and foglio:
        enrichment_steps.append(
            (
                "mappa",
                f"Mappa F.{foglio}",
                client.generic_search(
                    search_type="mappa",
                    provincia=provincia,
                    comune=comune,
                    tipo_catasto=tipo_catasto or "T",
                    foglio=foglio,
                ),
            )
        )
    if with_fiduciali and provincia and comune:
        enrichment_steps.append(
            (
                "fiduciali",
                f"Punti fiduciali {provincia}/{comune}",
                client.generic_search(
                    search_type="fiduciali",
                    provincia=provincia,
                    comune=comune,
                    tipo_catasto=tipo_catasto or "T",
                    foglio=foglio,
                ),
            )
        )
    if with_originali and provincia and comune:
        enrichment_steps.append(
            (
                "originali",
                f"Originali di impianto {provincia}/{comune}",
                client.generic_search(
                    search_type="originali",
                    provincia=provincia,
                    comune=comune,
                    tipo_catasto=tipo_catasto or "T",
                    foglio=foglio,
                ),
            )
        )
    if with_nota and provincia and numero_nota:
        enrichment_steps.append(
            (
                "nota",
                f"Nota {numero_nota}",
                client.generic_search(
                    search_type="nota", provincia=provincia, tipo_catasto=tipo_catasto or "T", numero_nota=numero_nota
                ),
            )
        )
    if with_ispezioni and provincia and comune:
        enrichment_steps.append(
            (
                "ispezioni",
                f"Ispezioni {provincia}/{comune}",
                client.generic_search(
                    search_type="ispezioni",
                    provincia=provincia,
                    comune=comune,
                    tipo_catasto=tipo_catasto or "T",
                    foglio=foglio,
                    particella=particella,
                ),
            )
        )
    if with_ispezioni_cart and provincia and comune:
        enrichment_steps.append(
            (
                "ispezioni_cartacee",
                f"Ispezioni cartacee {provincia}/{comune}",
                client.generic_search(
                    search_type="ispezioni_cart",
                    provincia=provincia,
                    comune=comune,
                    tipo_catasto=tipo_catasto or "T",
                    foglio=foglio,
                    particella=particella,
                ),
            )
        )

    # Also add soggetto/azienda as enrichment if not already run as starting phase
    if codice_fiscale and not is_person_start:
        enrichment_steps.append(
            (
                "soggetto",
                f"Soggetto CF={codice_fiscale}",
                client.soggetto(codice_fiscale=codice_fiscale, tipo_catasto=tipo_catasto, provincia=provincia),
            )
        )
    if azienda_id and not is_company_start:
        enrichment_steps.append(
            (
                "persona_giuridica",
                f"Persona giuridica ID={azienda_id}",
                client.persona_giuridica(identificativo=azienda_id, tipo_catasto=tipo_catasto, provincia=provincia),
            )
        )

    if enrichment_steps:
        console.rule("[bold cyan]Enrichment[/bold cyan]")
        for key, label, coro in enrichment_steps:
            all_data[key] = _run_step(client, label, coro)

    # -- Summary --------------------------------------------------------------

    console.rule("[bold green]Workflow complete[/bold green]")
    parts = []
    if "immobili" in all_data:
        parts.append(f"Immobili: [cyan]{len(all_data['immobili'])}[/cyan]")
    if intestati_results:
        ok = sum(1 for r in intestati_results if r.get("status") == "completed")
        parts.append(f"Intestati: [green]{ok}[/green]/{len(intestati_results)}")
    for key in (
        "soggetto",
        "persona_giuridica",
        "elenco_immobili",
        "mappa",
        "fiduciali",
        "originali",
        "nota",
        "ispezioni",
        "ispezioni_cartacee",
        "indirizzo",
    ):
        if key in all_data and isinstance(all_data[key], dict):
            s = all_data[key].get("status", "?")
            style = "green" if s == "completed" else "red"
            parts.append(f"{key}: [{style}]{s}[/{style}]")
    console.print("  " + "  ".join(parts))

    if output:
        _write_output(all_data, output)


# -- batch command (supports all query types) ---------------------------------

# Maps CSV 'command' column values to (client_method_name, required_fields, extra_field_mapping)
@query_app.command()
def batch(
    input_file: str = typer.Option(..., "--input", "-I", help="CSV file with query rows"),
    command: str = typer.Option(
        "search",
        "--command",
        "-c",
        help="Query command (any `sister query` single-step command) or 'auto' to read it from the CSV 'command' column",
    ),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for each result before submitting the next"),
    output_dir: Optional[str] = typer.Option(None, "--output-dir", "-O", help="Directory — writes one JSON per row"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Single output file (all results merged)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview rows without executing"),
    force: bool = typer.Option(False, "--force", help="Bypass cache, always submit new request"),
):
    """Submit multiple queries from a CSV file.

    Supports all query types. Use --command to set the type for all rows,
    or add a 'command' column in the CSV for per-row dispatch.

    The columns are the parameters of the command (see ``sister query <command> --help``, the same inputs
    as the SISTER form); columns that are not parameters of the command are ignored, so a CSV can carry
    extra metadata. Missing required parameters are reported per row.

    \b
    Example CSV (search):
        provincia,comune,foglio,particella,tipo_catasto
        Trieste,TRIESTE,9,166,F
        Roma,ROMA,100,50,T

    \b
    Example CSV (mixed, using 'command' column):
        command,provincia,comune,foglio,particella,codice_fiscale,tipo_catasto
        search,Trieste,TRIESTE,9,166,,F
        soggetto,,,,,RSSMRA85M01H501Z,
    """
    import os

    path = Path(input_file)
    if not path.exists():
        console.print(f"[red]File not found: {input_file}[/red]")
        raise typer.Exit(1)

    rows = []
    with path.open(encoding="utf-8") as fh:
        lines = [line for line in fh if not line.strip().startswith("#")]
        reader = csv.DictReader(io.StringIO("".join(lines)))
        for row in reader:
            row = {k.strip().lower(): v.strip() for k, v in row.items() if v and v.strip()}
            if row:
                rows.append(row)

    if not rows:
        console.print("[red]No valid rows found in input file.[/red]")
        raise typer.Exit(1)

    console.print(f"Loaded [cyan]{len(rows)}[/cyan] row(s) from {path.name}")

    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] — requests will not be sent")
        for i, row in enumerate(rows, 1):
            cmd = row.get("command", command)
            console.print(f"  {i}. [{cmd}] {' '.join(f'{k}={v}' for k, v in row.items() if k != 'command')}")
        return

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    client = VisuraClient()
    all_results = []
    ok_count = 0
    err_count = 0

    for i, row in enumerate(rows, 1):
        cmd = row.pop("command", command)
        input_metadata = {
            key: row[key]
            for key in ("source_files", "scope_note")
            if row.get(key)
        }
        spec = get_query_form(cmd)

        if spec is None:
            console.print(f"  [red]({i}/{len(rows)}) Unknown command: {cmd}[/red]")
            err_count += 1
            all_results.append(
                {"row": i, "command": cmd, **input_metadata, "status": "error", "error": f"Unknown command: {cmd}"}
            )
            continue

        # the CSV columns that are parameters of the query (anything else is row metadata)
        params = {name: row[name] for name in param_names(spec.command) if name in row}
        try:
            validate_params(spec.command, params)
        except ValueError as e:
            console.print(f"  [red]({i}/{len(rows)}) [{cmd}] {e}[/red]")
            err_count += 1
            all_results.append({"row": i, "command": cmd, **input_metadata, "status": "error", "error": str(e)})
            continue

        label = f"[{cmd}] " + " ".join(f"{k}={v}" for k, v in list(row.items())[:4])
        console.print(f"\n[dim]({i}/{len(rows)})[/dim] {label}")

        try:
            result = asyncio.run(client.submit(spec.command, params, force=force))
        except VisuraAPIError as e:
            console.print(f"  [red]Submit failed: HTTP {e.status_code}: {e.detail}[/red]")
            err_count += 1
            all_results.append({"row": i, "command": cmd, "label": label, "status": "error", "error": str(e)})
            continue

        # Extract request_id(s)
        request_ids = result.get("request_ids", [])
        if not request_ids:
            rid = result.get("request_id", "")
            if rid:
                request_ids = [rid]

        console.print(f"  Submitted: {', '.join(request_ids)}")

        if wait and request_ids:
            row_results = {}
            for rid in request_ids:
                try:
                    res = asyncio.run(client.wait_for_result(rid))
                    row_results[rid] = res
                    _print_result(res)
                except TimeoutError as e:
                    console.print(f"  [yellow]{e}[/yellow]")
                    row_results[rid] = {"status": "timeout"}
                except VisuraAPIError as e:
                    console.print(f"  [red]{rid}: HTTP {e.status_code}: {e.detail}[/red]")
                    row_results[rid] = {"status": "error"}

            entry = {
                "row": i,
                "command": cmd,
                **input_metadata,
                "label": label,
                "request_ids": request_ids,
                "results": row_results,
            }
            all_results.append(entry)

            if output_dir:
                row_file = os.path.join(output_dir, f"batch_{i:04d}_{cmd}.json")
                _write_output(entry, row_file)

            if all(r.get("status") == "completed" for r in row_results.values()):
                ok_count += 1
            else:
                err_count += 1
        else:
            all_results.append(
                {
                    "row": i,
                    "command": cmd,
                    **input_metadata,
                    "label": label,
                    "request_ids": request_ids,
                    "status": "queued",
                }
            )
            ok_count += 1

    console.rule("[bold green]Batch complete[/bold green]")
    console.print(
        f"  Total: [cyan]{len(rows)}[/cyan]  " f"OK: [green]{ok_count}[/green]  " f"Errors: [red]{err_count}[/red]"
    )

    if output:
        _write_output(all_results, output)


# =============================================================================
# top-level commands
# =============================================================================


@app.command()
def queries():
    """List available sister endpoints."""
    table = Table(title="Visura API endpoints", header_style="bold cyan")
    table.add_column("Command", style="cyan", no_wrap=True)
    table.add_column("Method", style="dim", no_wrap=True)
    table.add_column("Endpoint", style="white")
    table.add_column("Description")

    # one row per single-step query, straight from the spec that also generates the commands
    rows = [(f"query {form.command}", "POST", form.path, form.summary) for form in QUERY_FORMS.values()]
    rows += [
        ("query workflow", "—", "search → intestati", "Full two-phase: immobili + intestati"),
        ("query batch", "POST", "/visura (×N)", "Batch search from CSV file"),
        ("get", "GET", "/visura/{request_id}", "Poll for a single result"),
        ("wait", "GET", "/visura/{request_id}", "Poll until complete or timeout"),
        ("requests", "GET", "/visura/history", "List all requests with status"),
        ("history", "GET", "/visura/history", "Query response history"),
        ("health", "GET", "/health", "Service health check"),
    ]
    for cmd, method, ep, desc in rows:
        table.add_row(cmd, method, ep, desc)

    console.print(table)

    client = VisuraClient()
    console.print(f"[dim]Service URL: {client.base_url}[/dim]")


@app.command("get")
def get_result(
    request_id: str = typer.Argument(help="Request ID to retrieve"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path (.json)"),
):
    """Get the result of a visura request by ID (GET /visura/{request_id}).

    Returns the current status: processing, completed, error, or expired.
    """
    client = VisuraClient()

    try:
        result = asyncio.run(client.get_result(request_id))
    except VisuraAPIError as e:
        _handle_api_error(e)
        return

    _print_result(result)

    if output:
        _write_output(result, output)


@app.command("wait")
def wait_cmd(
    request_id: str = typer.Argument(help="Request ID to wait for"),
    timeout: Optional[float] = typer.Option(None, "--timeout", "-T", help="Max seconds to wait (default: from env)"),
    interval: Optional[float] = typer.Option(None, "--interval", help="Seconds between polls (default: from env)"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path (.json)"),
):
    """Poll a request until it completes or times out.

    Continuously polls GET /visura/{request_id} until status is
    'completed' or 'error', then prints the result.
    """
    client = VisuraClient()
    start = time.monotonic()

    console.print(f"[dim]Waiting for {request_id}...[/dim]")

    try:
        result = asyncio.run(client.wait_for_result(request_id, poll_interval=interval, poll_timeout=timeout))
    except TimeoutError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1) from None
    except VisuraAPIError as e:
        _handle_api_error(e)
        return

    elapsed = time.monotonic() - start
    status = result.get("status", "unknown")
    if status == "completed":
        console.print(f"[dim]Completed in {elapsed:.1f}s[/dim]")
    else:
        console.print(f"[dim]Finished in {elapsed:.1f}s (status: {status})[/dim]")
    _print_result(result)

    if output:
        _write_output(result, output)


@app.command()
def requests(
    provincia: Optional[str] = typer.Option(None, "--provincia", "-P", help="Filter by province"),
    comune: Optional[str] = typer.Option(None, "--comune", "-C", help="Filter by municipality"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Filter by sheet number"),
    particella: Optional[str] = typer.Option(None, "--particella", "-p", help="Filter by parcel"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="Filter by type (T/F)"),
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filter: completed, pending, failed"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max results to return"),
    offset: int = typer.Option(0, "--offset", help="Offset for pagination"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path (.json)"),
):
    """List all requests and their status from the database.

    Shows both requests that have a response (completed/failed) and those
    still pending. Use --status to filter.
    """
    _VALID_STATUSES = {"completed", "pending", "failed"}
    if status and status.lower() not in _VALID_STATUSES:
        console.print(f"[red]Invalid --status '{status}'. Must be one of: {', '.join(sorted(_VALID_STATUSES))}[/red]")
        raise typer.Exit(1)

    client = VisuraClient()

    try:
        result = asyncio.run(
            client.history(
                provincia=provincia,
                comune=comune,
                foglio=foglio,
                particella=particella,
                tipo_catasto=tipo_catasto,
                limit=limit,
                offset=offset,
            )
        )
    except VisuraAPIError as e:
        _handle_api_error(e)
        return

    items = result.get("results", [])

    if status:
        status_lower = status.lower()
        filtered = []
        for r in items:
            success = r.get("success")
            responded = r.get("responded_at")
            if status_lower == "completed" and success is True:
                filtered.append(r)
            elif status_lower == "failed" and success is False:
                filtered.append(r)
            elif status_lower == "pending" and responded is None:
                filtered.append(r)
        items = filtered

    if not items:
        console.print("[dim]No requests found.[/dim]")
        return

    table = Table(title=f"Requests ({len(items)} shown)", header_style="bold cyan")
    table.add_column("#", style="dim", justify="right", no_wrap=True)
    table.add_column("Request ID", style="green", no_wrap=True)
    table.add_column("Type", style="cyan", no_wrap=True)
    table.add_column("Cat.", style="cyan", no_wrap=True)
    table.add_column("Provincia")
    table.add_column("Comune")
    table.add_column("F.", no_wrap=True)
    table.add_column("P.", no_wrap=True)
    table.add_column("Sub.", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Submitted", style="dim", no_wrap=True)

    for i, r in enumerate(items, 1 + offset):
        success = r.get("success")
        responded = r.get("responded_at")

        if success is True:
            status_str = "[green]completed[/green]"
        elif success is False:
            status_str = "[red]failed[/red]"
        elif responded is None:
            status_str = "[yellow]pending[/yellow]"
        else:
            status_str = "[dim]-[/dim]"

        table.add_row(
            str(i),
            r.get("request_id", "-"),
            r.get("request_type", "-"),
            r.get("tipo_catasto", "-"),
            r.get("provincia", "-"),
            r.get("comune", "-"),
            r.get("foglio", "-"),
            r.get("particella", "-"),
            r.get("subalterno") or "-",
            status_str,
            r.get("requested_at", r.get("created_at", "-")),
        )

    console.print(table)

    if output:
        _write_output({"count": len(items), "results": items}, output)


@app.command()
def history(
    provincia: Optional[str] = typer.Option(None, "--provincia", "-P", help="Filter by province"),
    comune: Optional[str] = typer.Option(None, "--comune", "-C", help="Filter by municipality"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Filter by sheet number"),
    particella: Optional[str] = typer.Option(None, "--particella", "-p", help="Filter by parcel"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="Filter by type (T/F)"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max results to return"),
    offset: int = typer.Option(0, "--offset", help="Offset for pagination"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path (.json)"),
):
    """Query visura response history from the database."""
    client = VisuraClient()

    try:
        result = asyncio.run(
            client.history(
                provincia=provincia,
                comune=comune,
                foglio=foglio,
                particella=particella,
                tipo_catasto=tipo_catasto,
                limit=limit,
                offset=offset,
            )
        )
    except VisuraAPIError as e:
        _handle_api_error(e)
        return

    items = result.get("results", [])
    count = result.get("count", len(items))

    if not items:
        console.print("[dim]No history records found.[/dim]")
        return

    table = Table(title=f"Visura history ({count} results)", header_style="bold cyan")
    table.add_column("#", style="dim", justify="right", no_wrap=True)
    table.add_column("Request ID", style="green", no_wrap=True)
    table.add_column("Cat.", style="cyan", no_wrap=True)
    table.add_column("Provincia", style="white")
    table.add_column("Comune", style="white")
    table.add_column("Foglio", style="white", no_wrap=True)
    table.add_column("Particella", style="white", no_wrap=True)
    table.add_column("Success", style="yellow", no_wrap=True)
    table.add_column("Created", style="dim", no_wrap=True)

    for i, r in enumerate(items, 1 + offset):
        success = r.get("success")
        success_str = "[green]yes[/green]" if success else "[red]no[/red]" if success is not None else "[dim]-[/dim]"
        table.add_row(
            str(i),
            r.get("request_id", "-"),
            r.get("tipo_catasto", "-"),
            r.get("provincia", "-"),
            r.get("comune", "-"),
            r.get("foglio", "-"),
            r.get("particella", "-"),
            success_str,
            r.get("requested_at", r.get("created_at", "-")),
        )

    console.print(table)

    if output:
        _write_output(result, output)


@app.command()
def health():
    """Check sister service health (GET /health)."""
    client = VisuraClient()

    try:
        result = asyncio.run(client.health())
    except VisuraAPIError as e:
        _handle_api_error(e)
        return
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] Cannot reach sister at {client.base_url}: {e}")
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
    console.print(f"[dim]Service URL: {client.base_url}[/dim]")


# =============================================================================
# db subcommands
# =============================================================================


@db_app.command("init")
def db_init():
    """Initialize database and run all migrations."""
    import os

    from alembic.config import Config

    from alembic import command

    ini_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")
    cfg = Config(ini_path)
    command.upgrade(cfg, "head")
    console.print("[green]Database initialized and migrations applied.[/green]")


@db_app.command("migrate")
def db_migrate():
    """Run pending Alembic migrations."""
    import os

    from alembic.config import Config

    from alembic import command

    ini_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")
    cfg = Config(ini_path)
    command.upgrade(cfg, "head")
    console.print("[green]Migrations applied.[/green]")


@db_app.command("status")
def db_status():
    """Show current database migration revision."""
    import os

    from alembic.config import Config

    from alembic import command

    ini_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")
    cfg = Config(ini_path)
    command.current(cfg, verbose=True)


# -- entry point --------------------------------------------------------------


def run():
    """Entry point for the sister CLI."""
    app()


if __name__ == "__main__":
    run()
