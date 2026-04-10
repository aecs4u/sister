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
import io
import json
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .client import VisuraAPIError, VisuraClient

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


@query_app.command()
def search(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name (e.g. Trieste)"),
    comune: str = typer.Option(..., "--comune", "-C", help="Municipality name (e.g. TRIESTE)"),
    foglio: str = typer.Option(..., "--foglio", "-F", help="Sheet number"),
    particella: str = typer.Option(..., "--particella", "-p", help="Parcel number"),
    tipo_catasto: Optional[str] = typer.Option(
        None, "--tipo-catasto", "-t", help="'T' = Terreni, 'F' = Fabbricati (omit for both)"
    ),
    sezione: Optional[str] = typer.Option(None, "--sezione", help="Section (optional)"),
    subalterno: Optional[str] = typer.Option(None, "--subalterno", "-sub", help="Sub-unit (optional)"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path (.json)"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for results instead of returning immediately"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview request without executing"),
):
    """Submit an immobili search on SISTER (POST /visura).

    By default returns the queued request IDs. Use --wait to poll
    until results are ready.
    """
    payload = {
        "provincia": provincia,
        "comune": comune,
        "foglio": foglio,
        "particella": particella,
    }
    if tipo_catasto:
        payload["tipo_catasto"] = tipo_catasto.upper()
    if sezione:
        payload["sezione"] = sezione
    if subalterno:
        payload["subalterno"] = subalterno

    client = VisuraClient()

    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] — request will not be sent")
        console.print(f"  POST {client.base_url}/visura")
        console.print(f"  Body: {json.dumps(payload, ensure_ascii=False)}")
        return

    try:
        result = asyncio.run(
            client.search(
                provincia=provincia,
                comune=comune,
                foglio=foglio,
                particella=particella,
                tipo_catasto=tipo_catasto,
                sezione=sezione,
                subalterno=subalterno,
            )
        )
    except VisuraAPIError as e:
        _handle_api_error(e)
        return

    request_ids = result.get("request_ids", [])
    status = result.get("status", "unknown")

    console.print(f"[bold green]Request submitted[/bold green] (status: {status})")
    for rid in request_ids:
        console.print(f"  ID: [cyan]{rid}[/cyan]")

    if not wait:
        console.print(
            "[dim]Poll results with:[/dim]\n"
            + "\n".join(f"  [bold]sister get {rid}[/bold]" for rid in request_ids)
        )
        console.print(
            "[dim]Or wait automatically:[/dim]\n"
            + "\n".join(f"  [bold]sister wait {rid}[/bold]" for rid in request_ids)
        )
        return

    # --wait: poll each request_id until done
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
            console.print(f"[red]{rid}: HTTP {e.status_code}: {e.detail}[/red]")

    if output and all_results:
        merged = all_results if len(all_results) > 1 else next(iter(all_results.values()))
        _write_output(merged, output)


@query_app.command()
def intestati(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name"),
    comune: str = typer.Option(..., "--comune", "-C", help="Municipality name"),
    foglio: str = typer.Option(..., "--foglio", "-F", help="Sheet number"),
    particella: str = typer.Option(..., "--particella", "-p", help="Parcel number"),
    tipo_catasto: str = typer.Option(..., "--tipo-catasto", "-t", help="'T' = Terreni, 'F' = Fabbricati"),
    subalterno: Optional[str] = typer.Option(None, "--subalterno", "-sub", help="Sub-unit (required for Fabbricati)"),
    sezione: Optional[str] = typer.Option(None, "--sezione", help="Section (optional)"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path (.json)"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result instead of returning immediately"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview request without executing"),
):
    """Submit an owners (intestati) lookup on SISTER (POST /visura/intestati).

    For Fabbricati (tipo_catasto=F), --subalterno is required.
    For Terreni (tipo_catasto=T), --subalterno must not be provided.
    """
    payload = {
        "provincia": provincia,
        "comune": comune,
        "foglio": foglio,
        "particella": particella,
        "tipo_catasto": tipo_catasto.upper(),
    }
    if subalterno:
        payload["subalterno"] = subalterno
    if sezione:
        payload["sezione"] = sezione

    client = VisuraClient()

    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] — request will not be sent")
        console.print(f"  POST {client.base_url}/visura/intestati")
        console.print(f"  Body: {json.dumps(payload, ensure_ascii=False)}")
        return

    try:
        result = asyncio.run(
            client.intestati(
                provincia=provincia,
                comune=comune,
                foglio=foglio,
                particella=particella,
                tipo_catasto=tipo_catasto,
                subalterno=subalterno,
                sezione=sezione,
            )
        )
    except VisuraAPIError as e:
        _handle_api_error(e)
        return

    request_id = result.get("request_id", "")
    status = result.get("status", "unknown")

    console.print(f"[bold green]Request submitted[/bold green] (status: {status})")
    console.print(f"  ID: [cyan]{request_id}[/cyan]")

    if not wait:
        console.print(f"[dim]Poll result with:[/dim]\n  [bold]sister get {request_id}[/bold]")
        return

    console.print(f"\n[dim]Waiting for {request_id}...[/dim]")
    try:
        res = asyncio.run(client.wait_for_result(request_id))
        _print_result(res)
        if output:
            _write_output(res, output)
    except TimeoutError as e:
        console.print(f"[yellow]{e}[/yellow]")
    except VisuraAPIError as e:
        _handle_api_error(e)


@query_app.command()
def workflow(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name (e.g. Trieste)"),
    comune: str = typer.Option(..., "--comune", "-C", help="Municipality name (e.g. TRIESTE)"),
    foglio: str = typer.Option(..., "--foglio", "-F", help="Sheet number"),
    particella: str = typer.Option(..., "--particella", "-p", help="Parcel number"),
    tipo_catasto: Optional[str] = typer.Option(
        None, "--tipo-catasto", "-t", help="'T' = Terreni, 'F' = Fabbricati (omit for both)"
    ),
    sezione: Optional[str] = typer.Option(None, "--sezione", help="Section (optional)"),
    subalterno: Optional[str] = typer.Option(
        None, "--subalterno", "-sub", help="Limit intestati to this sub-unit (default: all found)"
    ),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path (.json)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview request without executing"),
):
    """Full two-phase workflow: search immobili then fetch intestati.

    Phase 1: Search for all immobili on the given particella.
    Phase 2: For each Fabbricato sub-unit found (or Terreni), automatically
    submit an intestati lookup and wait for results.

    Use --subalterno to restrict Phase 2 to a single sub-unit.
    """
    client = VisuraClient()

    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] — workflow preview")
        console.print(f"  Phase 1: POST {client.base_url}/visura")
        payload: dict = {
            "provincia": provincia, "comune": comune,
            "foglio": foglio, "particella": particella,
        }
        if tipo_catasto:
            payload["tipo_catasto"] = tipo_catasto.upper()
        if sezione:
            payload["sezione"] = sezione
        console.print(f"  Body: {json.dumps(payload, ensure_ascii=False)}")
        console.print(f"  Phase 2: POST {client.base_url}/visura/intestati (for each sub-unit found)")
        return

    # -- Phase 1: search immobili ---------------------------------------------

    console.rule("[bold cyan]Phase 1 — Search immobili[/bold cyan]")

    try:
        search_result = asyncio.run(
            client.search(
                provincia=provincia, comune=comune, foglio=foglio,
                particella=particella, tipo_catasto=tipo_catasto, sezione=sezione,
            )
        )
    except VisuraAPIError as e:
        _handle_api_error(e)
        return

    request_ids = search_result.get("request_ids", [])
    console.print(f"Submitted {len(request_ids)} request(s)")

    search_results = {}
    for rid in request_ids:
        console.print(f"  [dim]Waiting for {rid}...[/dim]")
        try:
            res = asyncio.run(client.wait_for_result(rid))
            search_results[rid] = res
            _print_result(res)
        except TimeoutError as e:
            console.print(f"[yellow]{e}[/yellow]")
        except VisuraAPIError as e:
            console.print(f"[red]{rid}: HTTP {e.status_code}: {e.detail}[/red]")

    # -- Collect immobili and determine intestati targets ----------------------

    all_immobili = []
    for res in search_results.values():
        if res.get("status") != "completed":
            continue
        data = res.get("data", {})
        if isinstance(data, dict):
            all_immobili.extend(data.get("immobili", []))

    if not all_immobili:
        console.print("[yellow]No immobili found — skipping Phase 2.[/yellow]")
        if output:
            _write_output({"immobili": [], "intestati_results": []}, output)
        return

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
            subs_found = set()
            for imm in immobili:
                sub = imm.get("Sub", "").strip()
                if sub:
                    subs_found.add(sub)

            if subalterno:
                intestati_targets.append(("F", subalterno))
            elif subs_found:
                for sub in sorted(subs_found):
                    intestati_targets.append(("F", sub))
            else:
                console.print("[yellow]No sub-units found in Fabbricati results — skipping intestati.[/yellow]")

    if not intestati_targets:
        console.print("[dim]No intestati targets identified.[/dim]")
        if output:
            _write_output({"immobili": all_immobili, "intestati_results": []}, output)
        return

    # -- Phase 2: fetch intestati for each target -----------------------------

    console.rule("[bold cyan]Phase 2 — Fetch intestati[/bold cyan]")
    console.print(f"Submitting {len(intestati_targets)} intestati request(s)")

    intestati_results = []
    for tc, sub in intestati_targets:
        sub_label = f" Sub.{sub}" if sub else ""
        console.print(f"\n  [dim]{tc}{sub_label}...[/dim]")

        try:
            submit = asyncio.run(
                client.intestati(
                    provincia=provincia, comune=comune, foglio=foglio,
                    particella=particella, tipo_catasto=tc,
                    subalterno=sub, sezione=sezione,
                )
            )
        except VisuraAPIError as e:
            console.print(f"  [red]Submit failed: HTTP {e.status_code}: {e.detail}[/red]")
            intestati_results.append({"tipo_catasto": tc, "subalterno": sub, "status": "error", "error": str(e)})
            continue

        rid = submit.get("request_id", "")
        console.print(f"  ID: [cyan]{rid}[/cyan]")

        try:
            res = asyncio.run(client.wait_for_result(rid))
            _print_result(res)
            intestati_results.append({
                "tipo_catasto": tc,
                "subalterno": sub,
                "request_id": rid,
                **res,
            })
        except TimeoutError as e:
            console.print(f"  [yellow]{e}[/yellow]")
            intestati_results.append({"tipo_catasto": tc, "subalterno": sub, "request_id": rid, "status": "timeout"})
        except VisuraAPIError as e:
            console.print(f"  [red]{rid}: HTTP {e.status_code}: {e.detail}[/red]")
            intestati_results.append({"tipo_catasto": tc, "subalterno": sub, "request_id": rid, "status": "error"})

    # -- Summary --------------------------------------------------------------

    console.rule("[bold green]Workflow complete[/bold green]")
    succeeded = sum(1 for r in intestati_results if r.get("status") == "completed")
    console.print(
        f"  Immobili: [cyan]{len(all_immobili)}[/cyan]  "
        f"Intestati requests: [cyan]{len(intestati_results)}[/cyan]  "
        f"Succeeded: [green]{succeeded}[/green]"
    )

    if output:
        _write_output({
            "immobili": all_immobili,
            "intestati_results": intestati_results,
        }, output)


@query_app.command()
def batch(
    input_file: str = typer.Option(..., "--input", "-I", help="CSV file: provincia,comune,foglio,particella[,tipo_catasto][,subalterno]"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for each result before submitting the next"),
    output_dir: Optional[str] = typer.Option(None, "--output-dir", "-O", help="Directory — writes one JSON per row"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Single output file (all results merged)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview rows without executing"),
):
    """Submit multiple searches from a CSV file.

    The CSV must have columns: provincia, comune, foglio, particella.
    Optional columns: tipo_catasto, subalterno, sezione.
    Lines starting with # are ignored.

    \b
    Example CSV:
        provincia,comune,foglio,particella,tipo_catasto
        Trieste,TRIESTE,9,166,F
        Roma,ROMA,100,50,T
    """
    import os

    # -- Parse input file ---------------------------------------------------------

    path = Path(input_file)
    if not path.exists():
        console.print(f"[red]File not found: {input_file}[/red]")
        raise typer.Exit(1)

    rows = []
    with path.open(encoding="utf-8") as fh:
        # Filter comment lines before passing to csv reader
        lines = [line for line in fh if not line.strip().startswith("#")]
        reader = csv.DictReader(io.StringIO("".join(lines)))
        for row in reader:
            # Normalise keys to lowercase and strip whitespace
            row = {k.strip().lower(): v.strip() for k, v in row.items() if v and v.strip()}
            if not all(k in row for k in ("provincia", "comune", "foglio", "particella")):
                continue
            rows.append(row)

    if not rows:
        console.print("[red]No valid rows found in input file.[/red]")
        raise typer.Exit(1)

    console.print(f"Loaded [cyan]{len(rows)}[/cyan] row(s) from {path.name}")

    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] — requests will not be sent")
        for i, row in enumerate(rows, 1):
            tc = row.get("tipo_catasto", "T+F")
            sub = row.get("subalterno", "")
            console.print(
                f"  {i}. {row['provincia']}/{row['comune']} "
                f"F.{row['foglio']} P.{row['particella']} "
                f"tipo={tc}" + (f" sub={sub}" if sub else "")
            )
        return

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    # -- Submit each row ----------------------------------------------------------

    client = VisuraClient()
    all_results = []
    ok_count = 0
    err_count = 0

    for i, row in enumerate(rows, 1):
        tc = row.get("tipo_catasto")
        sub = row.get("subalterno")
        sez = row.get("sezione")
        label = f"{row['provincia']}/{row['comune']} F.{row['foglio']} P.{row['particella']}"

        console.print(f"\n[dim]({i}/{len(rows)})[/dim] {label}" + (f" {tc}" if tc else "") + (f" sub={sub}" if sub else ""))

        try:
            result = asyncio.run(
                client.search(
                    provincia=row["provincia"],
                    comune=row["comune"],
                    foglio=row["foglio"],
                    particella=row["particella"],
                    tipo_catasto=tc,
                    subalterno=sub,
                    sezione=sez,
                )
            )
        except VisuraAPIError as e:
            console.print(f"  [red]Submit failed: HTTP {e.status_code}: {e.detail}[/red]")
            err_count += 1
            all_results.append({"row": i, "label": label, "status": "error", "error": str(e)})
            continue

        request_ids = result.get("request_ids", [])
        console.print(f"  Submitted: {', '.join(request_ids)}")

        if wait:
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

            entry = {"row": i, "label": label, "request_ids": request_ids, "results": row_results}
            all_results.append(entry)

            if output_dir:
                row_file = os.path.join(output_dir, f"batch_{i:04d}_{row['foglio']}_{row['particella']}.json")
                _write_output(entry, row_file)

            if all(r.get("status") == "completed" for r in row_results.values()):
                ok_count += 1
            else:
                err_count += 1
        else:
            all_results.append({"row": i, "label": label, "request_ids": request_ids, "status": "queued"})
            ok_count += 1

    # -- Summary ------------------------------------------------------------------

    console.rule("[bold green]Batch complete[/bold green]")
    console.print(
        f"  Total: [cyan]{len(rows)}[/cyan]  "
        f"OK: [green]{ok_count}[/green]  "
        f"Errors: [red]{err_count}[/red]"
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

    rows = [
        ("query search", "POST", "/visura", "Submit immobili search (Fase 1)"),
        ("query intestati", "POST", "/visura/intestati", "Submit owners lookup (Fase 2)"),
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
        result = asyncio.run(
            client.wait_for_result(request_id, poll_interval=interval, poll_timeout=timeout)
        )
    except TimeoutError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1) from None
    except VisuraAPIError as e:
        _handle_api_error(e)
        return

    elapsed = time.monotonic() - start
    console.print(f"[dim]Completed in {elapsed:.1f}s[/dim]")
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
    table.add_column("Type", style="cyan", no_wrap=True)
    table.add_column("Provincia", style="white")
    table.add_column("Comune", style="white")
    table.add_column("Foglio", style="white", no_wrap=True)
    table.add_column("Particella", style="white", no_wrap=True)
    table.add_column("Success", style="yellow", no_wrap=True)
    table.add_column("Created", style="dim", no_wrap=True)

    for i, r in enumerate(items, 1 + offset):
        success = r.get("success")
        success_str = (
            "[green]yes[/green]" if success else "[red]no[/red]" if success is not None else "[dim]-[/dim]"
        )
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


# -- entry point --------------------------------------------------------------


def run():
    """Entry point for the sister CLI."""
    app()


if __name__ == "__main__":
    run()
