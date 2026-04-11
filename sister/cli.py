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
        if output:
            _write_output(result, output)
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
    tc = tipo_catasto.upper()
    if tc == "F" and not subalterno:
        console.print("[red]Error: --subalterno is required for Fabbricati (tipo_catasto=F)[/red]")
        raise typer.Exit(1)
    if tc == "T" and subalterno:
        console.print("[red]Error: --subalterno must not be provided for Terreni (tipo_catasto=T)[/red]")
        raise typer.Exit(1)

    payload = {
        "provincia": provincia,
        "comune": comune,
        "foglio": foglio,
        "particella": particella,
        "tipo_catasto": tc,
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
        if output:
            _write_output(result, output)
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
def soggetto(
    codice_fiscale: str = typer.Option(..., "--cf", "-i", help="Codice fiscale del soggetto"),
    tipo_catasto: Optional[str] = typer.Option(
        None, "--tipo-catasto", "-t", help="'T' = Terreni, 'F' = Fabbricati, 'E' = both (default)"
    ),
    provincia: Optional[str] = typer.Option(
        None, "--provincia", "-P", help="Province (omit for national search)"
    ),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path (.json)"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result instead of returning immediately"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview request without executing"),
):
    """National search by codice fiscale on SISTER (POST /visura/soggetto).

    Searches for all properties owned by a subject across Italy.
    Use --provincia to restrict to a single province.
    """
    payload = {"codice_fiscale": codice_fiscale.upper()}
    if tipo_catasto:
        payload["tipo_catasto"] = tipo_catasto.upper()
    if provincia:
        payload["provincia"] = provincia

    client = VisuraClient()

    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] — request will not be sent")
        console.print(f"  POST {client.base_url}/visura/soggetto")
        console.print(f"  Body: {json.dumps(payload, ensure_ascii=False)}")
        return

    try:
        result = asyncio.run(
            client.soggetto(
                codice_fiscale=codice_fiscale,
                tipo_catasto=tipo_catasto,
                provincia=provincia,
            )
        )
    except VisuraAPIError as e:
        _handle_api_error(e)
        return

    request_id = result.get("request_id", "")
    status = result.get("status", "unknown")
    scope = result.get("provincia", "NAZIONALE")

    console.print(f"[bold green]Request submitted[/bold green] (status: {status})")
    console.print(f"  ID: [cyan]{request_id}[/cyan]")
    console.print(f"  CF: {codice_fiscale.upper()}  Scope: {scope}")

    if not wait:
        console.print(f"[dim]Poll result with:[/dim]\n  [bold]sister get {request_id}[/bold]")
        if output:
            _write_output(result, output)
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
def azienda(
    identificativo: str = typer.Option(..., "--id", "-i", help="P.IVA (11 digits) or company name"),
    tipo_catasto: Optional[str] = typer.Option(
        None, "--tipo-catasto", "-t", help="'T' = Terreni, 'F' = Fabbricati, 'E' = both (default)"
    ),
    provincia: Optional[str] = typer.Option(
        None, "--provincia", "-P", help="Province (omit for national search)"
    ),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path (.json)"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview request without executing"),
):
    """Search by legal entity (P.IVA or company name) on SISTER (POST /visura/persona-giuridica).

    Searches for all properties owned by a company across Italy.
    Use --provincia to restrict to a single province.
    """
    payload = {"identificativo": identificativo}
    if tipo_catasto:
        payload["tipo_catasto"] = tipo_catasto.upper()
    if provincia:
        payload["provincia"] = provincia

    client = VisuraClient()

    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] — request will not be sent")
        console.print(f"  POST {client.base_url}/visura/persona-giuridica")
        console.print(f"  Body: {json.dumps(payload, ensure_ascii=False)}")
        return

    try:
        result = asyncio.run(
            client.persona_giuridica(
                identificativo=identificativo,
                tipo_catasto=tipo_catasto,
                provincia=provincia,
            )
        )
    except VisuraAPIError as e:
        _handle_api_error(e)
        return

    request_id = result.get("request_id", "")
    status = result.get("status", "unknown")
    scope = result.get("provincia", "NAZIONALE")

    console.print(f"[bold green]Request submitted[/bold green] (status: {status})")
    console.print(f"  ID: [cyan]{request_id}[/cyan]")
    console.print(f"  Identificativo: {identificativo}  Scope: {scope}")

    if not wait:
        console.print(f"[dim]Poll result with:[/dim]\n  [bold]sister get {request_id}[/bold]")
        if output:
            _write_output(result, output)
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
def elenco(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name"),
    comune: str = typer.Option(..., "--comune", "-C", help="Municipality name"),
    tipo_catasto: Optional[str] = typer.Option(
        None, "--tipo-catasto", "-t", help="'T' = Terreni, 'F' = Fabbricati"
    ),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Filter by sheet number"),
    sezione: Optional[str] = typer.Option(None, "--sezione", help="Section (optional)"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path (.json)"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview request without executing"),
):
    """List all properties in a comune (POST /visura/elenco-immobili).

    Optionally filter by foglio to narrow results.
    """
    payload = {"provincia": provincia, "comune": comune}
    if tipo_catasto:
        payload["tipo_catasto"] = tipo_catasto.upper()
    if foglio:
        payload["foglio"] = foglio
    if sezione:
        payload["sezione"] = sezione

    client = VisuraClient()

    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] — request will not be sent")
        console.print(f"  POST {client.base_url}/visura/elenco-immobili")
        console.print(f"  Body: {json.dumps(payload, ensure_ascii=False)}")
        return

    try:
        result = asyncio.run(
            client.elenco_immobili(
                provincia=provincia,
                comune=comune,
                tipo_catasto=tipo_catasto,
                foglio=foglio,
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
        if output:
            _write_output(result, output)
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


# -- generic SISTER search commands (IND, PART, NOTA, EM, EXPM, OOII, FID, ISP, ISPCART) --


def _generic_search_command(
    search_type: str,
    provincia: str,
    client: VisuraClient,
    wait: bool,
    output: Optional[str],
    comune: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    **params,
):
    """Shared logic for generic SISTER search CLI commands."""
    try:
        result = asyncio.run(
            client.generic_search(
                search_type=search_type,
                provincia=provincia,
                comune=comune,
                tipo_catasto=tipo_catasto,
                **params,
            )
        )
    except VisuraAPIError as e:
        _handle_api_error(e)
        return

    request_id = result.get("request_id", "")
    console.print(f"[bold green]Request submitted[/bold green] ({search_type})")
    console.print(f"  ID: [cyan]{request_id}[/cyan]")

    if not wait:
        console.print(f"[dim]Poll result with:[/dim]\n  [bold]sister get {request_id}[/bold]")
        if output:
            _write_output(result, output)
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
def indirizzo(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name"),
    comune: str = typer.Option(..., "--comune", "-C", help="Municipality name"),
    indirizzo_str: str = typer.Option(..., "--indirizzo", "-a", help="Street address to search"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="T/F"),
    sezione: Optional[str] = typer.Option(None, "--sezione", help="Section"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
):
    """Search by street address (IND) on SISTER."""
    client = VisuraClient()
    if dry_run:
        console.print(f"[bold yellow]DRY RUN[/bold yellow] POST /visura/indirizzo  {provincia}/{comune} '{indirizzo_str}'")
        return
    _generic_search_command("indirizzo", provincia, client, wait, output,
                            comune=comune, tipo_catasto=tipo_catasto, indirizzo=indirizzo_str)


@query_app.command()
def partita(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name"),
    comune: str = typer.Option(..., "--comune", "-C", help="Municipality name"),
    partita_num: str = typer.Option(..., "--partita", help="Partita catastale number"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="T/F"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
):
    """Search by partita catastale number (PART) on SISTER."""
    client = VisuraClient()
    if dry_run:
        console.print(f"[bold yellow]DRY RUN[/bold yellow] POST /visura/partita  {provincia}/{comune} P.{partita_num}")
        return
    _generic_search_command("partita", provincia, client, wait, output,
                            comune=comune, tipo_catasto=tipo_catasto, partita=partita_num)


@query_app.command()
def nota(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name"),
    numero_nota: str = typer.Option(..., "--numero", "-n", help="Note/annotation number"),
    anno_nota: Optional[str] = typer.Option(None, "--anno", help="Year of the note"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="T/F"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
):
    """Search by annotation/note reference (NOTA) on SISTER."""
    client = VisuraClient()
    if dry_run:
        console.print(f"[bold yellow]DRY RUN[/bold yellow] POST /visura/nota  {provincia} nota={numero_nota}")
        return
    _generic_search_command("nota", provincia, client, wait, output,
                            tipo_catasto=tipo_catasto, numero_nota=numero_nota, anno_nota=anno_nota)


@query_app.command()
def mappa(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name"),
    comune: str = typer.Option(..., "--comune", "-C", help="Municipality name"),
    foglio: str = typer.Option(..., "--foglio", "-F", help="Sheet number"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="T/F"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
):
    """View cadastral map data (EM) on SISTER."""
    client = VisuraClient()
    if dry_run:
        console.print(f"[bold yellow]DRY RUN[/bold yellow] POST /visura/mappa  {provincia}/{comune} F.{foglio}")
        return
    _generic_search_command("mappa", provincia, client, wait, output,
                            comune=comune, tipo_catasto=tipo_catasto, foglio=foglio)


@query_app.command("export-mappa")
def export_mappa(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name"),
    comune: str = typer.Option(..., "--comune", "-C", help="Municipality name"),
    foglio: str = typer.Option(..., "--foglio", "-F", help="Sheet number"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="T/F"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
):
    """Export cadastral map data (EXPM) on SISTER."""
    client = VisuraClient()
    if dry_run:
        console.print(f"[bold yellow]DRY RUN[/bold yellow] POST /visura/export-mappa  {provincia}/{comune} F.{foglio}")
        return
    _generic_search_command("export_mappa", provincia, client, wait, output,
                            comune=comune, tipo_catasto=tipo_catasto, foglio=foglio)


@query_app.command()
def originali(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name"),
    comune: str = typer.Option(..., "--comune", "-C", help="Municipality name"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="T/F"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Sheet number"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
):
    """Retrieve original registration records (OOII) on SISTER."""
    client = VisuraClient()
    if dry_run:
        console.print(f"[bold yellow]DRY RUN[/bold yellow] POST /visura/originali  {provincia}/{comune}")
        return
    _generic_search_command("originali", provincia, client, wait, output,
                            comune=comune, tipo_catasto=tipo_catasto, foglio=foglio)


@query_app.command()
def fiduciali(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name"),
    comune: str = typer.Option(..., "--comune", "-C", help="Municipality name"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="T/F"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Sheet number"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
):
    """Retrieve survey reference points (FID) on SISTER."""
    client = VisuraClient()
    if dry_run:
        console.print(f"[bold yellow]DRY RUN[/bold yellow] POST /visura/fiduciali  {provincia}/{comune}")
        return
    _generic_search_command("fiduciali", provincia, client, wait, output,
                            comune=comune, tipo_catasto=tipo_catasto, foglio=foglio)


@query_app.command()
def ispezioni(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name"),
    comune: str = typer.Option(..., "--comune", "-C", help="Municipality name"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="T/F"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Sheet number"),
    particella: Optional[str] = typer.Option(None, "--particella", "-p", help="Parcel number"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
):
    """Search property inspection records (ISP) on SISTER."""
    client = VisuraClient()
    if dry_run:
        console.print(f"[bold yellow]DRY RUN[/bold yellow] POST /visura/ispezioni  {provincia}/{comune}")
        return
    _generic_search_command("ispezioni", provincia, client, wait, output,
                            comune=comune, tipo_catasto=tipo_catasto, foglio=foglio, particella=particella)


@query_app.command("ispezioni-cartacee")
def ispezioni_cartacee(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Province name"),
    comune: str = typer.Option(..., "--comune", "-C", help="Municipality name"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="T/F"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Sheet number"),
    particella: Optional[str] = typer.Option(None, "--particella", "-p", help="Parcel number"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for result"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
):
    """Search paper inspection records (ISPCART) on SISTER."""
    client = VisuraClient()
    if dry_run:
        console.print(f"[bold yellow]DRY RUN[/bold yellow] POST /visura/ispezioni-cartacee  {provincia}/{comune}")
        return
    _generic_search_command("ispezioni_cart", provincia, client, wait, output,
                            comune=comune, tipo_catasto=tipo_catasto, foglio=foglio, particella=particella)


# -- Workflow presets ---------------------------------------------------------

_PRESETS = {
    "due-diligence": {
        "description": "Real estate due diligence: search → intestati → ispezioni",
        "requires": ("provincia", "comune", "foglio", "particella"),
        "steps": {"search": True, "intestati": True, "ispezioni": True},
    },
    "patrimonio": {
        "description": "Asset investigation: soggetto (nazionale) → for each property → intestati",
        "requires": ("codice_fiscale",),
        "steps": {"soggetto": True, "intestati_from_soggetto": True},
    },
    "fondiario": {
        "description": "Land survey: elenco immobili → mappa → fiduciali → originali",
        "requires": ("provincia", "comune"),
        "steps": {"elenco": True, "mappa": True, "fiduciali": True, "originali": True},
    },
    "aziendale": {
        "description": "Corporate property audit: azienda → for each property → search → intestati",
        "requires": ("azienda_id",),
        "steps": {"azienda": True, "intestati_from_soggetto": True},
    },
    "storico": {
        "description": "Parcel history: search → intestati → nota → ispezioni → ispezioni_cart → originali",
        "requires": ("provincia", "comune", "foglio", "particella"),
        "steps": {"search": True, "intestati": True, "nota": True, "ispezioni": True, "ispezioni_cart": True, "originali": True},
    },
    "indirizzo": {
        "description": "Address lookup: indirizzo → search → intestati",
        "requires": ("provincia", "comune", "indirizzo_str"),
        "steps": {"indirizzo_search": True, "search": True, "intestati": True},
    },
    "cross-reference": {
        "description": "Cross-reference: soggetto + azienda, compare property overlap",
        "requires": ("codice_fiscale", "azienda_id"),
        "steps": {"soggetto": True, "azienda": True},
    },
}


def _run_step(client, label, coro):
    """Run a single workflow step: submit → wait → print."""
    console.print(f"\n  [dim]{label}...[/dim]")
    try:
        submit = asyncio.run(coro)
        rid = submit.get("request_id", "")
        rids = submit.get("request_ids", [rid] if rid else [])
        for r in rids:
            console.print(f"  ID: [cyan]{r}[/cyan]")
        results = []
        for r in rids:
            res = asyncio.run(client.wait_for_result(r))
            _print_result(res)
            results.append(res)
        return results[0] if len(results) == 1 else results
    except (TimeoutError, VisuraAPIError) as e:
        console.print(f"  [red]{e}[/red]")
        return {"status": "error", "error": str(e)}


@query_app.command()
def workflow(
    preset: Optional[str] = typer.Option(
        None, "--preset",
        help="Named preset: due-diligence, patrimonio, fondiario, aziendale, storico, indirizzo, cross-reference",
    ),
    provincia: Optional[str] = typer.Option(None, "--provincia", "-P", help="Province name"),
    comune: Optional[str] = typer.Option(None, "--comune", "-C", help="Municipality name"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Sheet number"),
    particella: Optional[str] = typer.Option(None, "--particella", "-p", help="Parcel number"),
    tipo_catasto: Optional[str] = typer.Option(
        None, "--tipo-catasto", "-t", help="'T' = Terreni, 'F' = Fabbricati (omit for both)"
    ),
    sezione: Optional[str] = typer.Option(None, "--sezione", help="Section (optional)"),
    subalterno: Optional[str] = typer.Option(
        None, "--subalterno", "-sub", help="Limit intestati to this sub-unit"
    ),
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
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path (.json)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview steps without executing"),
):
    """Multi-phase workflow with optional preset.

    \b
    Without --preset: runs search → intestati plus any flags you enable.
    With --preset: runs a named sequence of steps automatically.

    \b
    Available presets:
      due-diligence   search → intestati → ispezioni
      patrimonio      soggetto (nazionale) → intestati per immobile
      fondiario       elenco → mappa → fiduciali → originali
      aziendale       azienda → intestati per immobile
      storico         search → intestati → nota → ispezioni → originali
      indirizzo       indirizzo → search → intestati
      cross-reference soggetto + azienda (compare overlap)

    \b
    Examples:
      sister query workflow --preset due-diligence -P Trieste -C TRIESTE -F 9 -p 166
      sister query workflow --preset patrimonio --cf RSSMRA85M01H501Z
      sister query workflow --preset fondiario -P Roma -C ROMA -F 100
      sister query workflow --preset aziendale --azienda 01234567890
      sister query workflow -P Trieste -C TRIESTE -F 9 -p 166 --elenco --mappa
    """
    # -- Apply preset flags ---------------------------------------------------

    if preset:
        if preset not in _PRESETS:
            console.print(f"[red]Unknown preset: {preset}[/red]")
            console.print("[dim]Available: " + ", ".join(_PRESETS.keys()) + "[/dim]")
            raise typer.Exit(1)

        p = _PRESETS[preset]
        steps = p["steps"]

        # Validate required fields
        field_map = {
            "provincia": provincia, "comune": comune, "foglio": foglio,
            "particella": particella, "codice_fiscale": codice_fiscale,
            "azienda_id": azienda_id, "indirizzo_str": indirizzo_str,
        }
        missing = [f for f in p["requires"] if not field_map.get(f)]
        if missing:
            flag_names = {"provincia": "-P", "comune": "-C", "foglio": "-F", "particella": "-p",
                          "codice_fiscale": "--cf", "azienda_id": "--azienda", "indirizzo_str": "--indirizzo"}
            console.print(f"[red]Preset '{preset}' requires: {', '.join(flag_names.get(f, f) for f in missing)}[/red]")
            raise typer.Exit(1)

        # Enable flags based on preset
        if steps.get("elenco"):
            with_elenco = True
        if steps.get("mappa"):
            with_mappa = True
        if steps.get("ispezioni"):
            with_ispezioni = True
        if steps.get("fiduciali"):
            with_fiduciali = True
        if steps.get("originali"):
            with_originali = True
        if steps.get("nota"):
            with_nota = True
        if steps.get("ispezioni_cart"):
            with_ispezioni_cart = True

        console.print(f"[bold]Preset: {preset}[/bold] — {p['description']}")

    client = VisuraClient()
    all_data = {"preset": preset} if preset else {}

    # -- Dry run preview ------------------------------------------------------

    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] — workflow preview")
        if preset:
            console.print(f"  Preset: {preset}")
        if codice_fiscale and (not preset or _PRESETS.get(preset, {}).get("steps", {}).get("soggetto")):
            console.print(f"  → soggetto CF={codice_fiscale}")
        if azienda_id and (not preset or _PRESETS.get(preset, {}).get("steps", {}).get("azienda")):
            console.print(f"  → azienda ID={azienda_id}")
        if indirizzo_str:
            console.print(f"  → indirizzo '{indirizzo_str}' {provincia}/{comune}")
        if foglio and particella:
            console.print(f"  → search {provincia}/{comune} F.{foglio} P.{particella}")
            console.print(f"  → intestati (for each sub-unit)")
        if with_elenco:
            console.print(f"  → elenco immobili {provincia}/{comune}")
        if with_mappa:
            console.print(f"  → mappa F.{foglio}")
        if with_ispezioni:
            console.print(f"  → ispezioni")
        if with_fiduciali:
            console.print(f"  → fiduciali")
        if with_originali:
            console.print(f"  → originali")
        if with_nota:
            console.print(f"  → nota")
        if with_ispezioni_cart:
            console.print(f"  → ispezioni cartacee")
        return

    # -- Phase: soggetto / azienda (if starting from person/company) ----------

    is_person_start = preset in ("patrimonio", "cross-reference") or (codice_fiscale and not foglio)
    is_company_start = preset in ("aziendale", "cross-reference") or (azienda_id and not foglio and not codice_fiscale)

    if codice_fiscale and (is_person_start or preset == "cross-reference"):
        console.rule("[bold cyan]Soggetto — National CF search[/bold cyan]")
        all_data["soggetto"] = _run_step(
            client, f"Soggetto CF={codice_fiscale}",
            client.soggetto(codice_fiscale=codice_fiscale, tipo_catasto=tipo_catasto, provincia=provincia),
        )

    if azienda_id and (is_company_start or preset == "cross-reference"):
        console.rule("[bold cyan]Azienda — Company search[/bold cyan]")
        all_data["persona_giuridica"] = _run_step(
            client, f"Persona giuridica ID={azienda_id}",
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
            client, f"Indirizzo '{indirizzo_str}' in {provincia}/{comune}",
            client.generic_search(search_type="indirizzo", provincia=provincia, comune=comune,
                                  tipo_catasto=tipo_catasto or "T", indirizzo=indirizzo_str),
        )

    # -- Phase: search immobili (if we have foglio/particella) ----------------

    all_immobili = []
    search_results = {}

    if foglio and particella and provincia and comune:
        console.rule("[bold cyan]Search — Immobili[/bold cyan]")
        try:
            search_result = asyncio.run(
                client.search(
                    provincia=provincia, comune=comune, foglio=foglio,
                    particella=particella, tipo_catasto=tipo_catasto, sezione=sezione,
                )
            )
            request_ids = search_result.get("request_ids", [])
            console.print(f"Submitted {len(request_ids)} request(s)")

            for rid in request_ids:
                console.print(f"  [dim]Waiting for {rid}...[/dim]")
                try:
                    res = asyncio.run(client.wait_for_result(rid))
                    search_results[rid] = res
                    _print_result(res)
                except (TimeoutError, VisuraAPIError) as e:
                    console.print(f"  [red]{e}[/red]")
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
    need_intestati = (not preset) or _PRESETS.get(preset, {}).get("steps", {}).get("intestati") or _PRESETS.get(preset, {}).get("steps", {}).get("search")

    if all_immobili and need_intestati and provincia and comune and foglio and particella:
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
                try:
                    submit = asyncio.run(
                        client.intestati(
                            provincia=provincia, comune=comune, foglio=foglio,
                            particella=particella, tipo_catasto=tc,
                            subalterno=sub, sezione=sezione,
                        )
                    )
                    rid = submit.get("request_id", "")
                    console.print(f"  [dim]{tc}{sub_label}[/dim] → [cyan]{rid}[/cyan]")
                    res = asyncio.run(client.wait_for_result(rid))
                    _print_result(res)
                    intestati_results.append({"tipo_catasto": tc, "subalterno": sub, "request_id": rid, **res})
                except (TimeoutError, VisuraAPIError) as e:
                    console.print(f"  [red]{tc}{sub_label}: {e}[/red]")
                    intestati_results.append({"tipo_catasto": tc, "subalterno": sub, "status": "error", "error": str(e)})

    all_data["intestati_results"] = intestati_results

    # -- Phase: enrichment (elenco, mappa, fiduciali, originali, etc.) --------

    enrichment_steps = []
    if with_elenco and provincia and comune:
        enrichment_steps.append(("elenco_immobili", f"Elenco immobili {provincia}/{comune}",
                                 client.elenco_immobili(provincia=provincia, comune=comune, tipo_catasto=tipo_catasto, foglio=foglio)))
    if with_mappa and provincia and comune and foglio:
        enrichment_steps.append(("mappa", f"Mappa F.{foglio}",
                                 client.generic_search(search_type="mappa", provincia=provincia, comune=comune, tipo_catasto=tipo_catasto or "T", foglio=foglio)))
    if with_fiduciali and provincia and comune:
        enrichment_steps.append(("fiduciali", f"Punti fiduciali {provincia}/{comune}",
                                 client.generic_search(search_type="fiduciali", provincia=provincia, comune=comune, tipo_catasto=tipo_catasto or "T", foglio=foglio)))
    if with_originali and provincia and comune:
        enrichment_steps.append(("originali", f"Originali di impianto {provincia}/{comune}",
                                 client.generic_search(search_type="originali", provincia=provincia, comune=comune, tipo_catasto=tipo_catasto or "T", foglio=foglio)))
    if with_nota and provincia and numero_nota:
        enrichment_steps.append(("nota", f"Nota {numero_nota}",
                                 client.generic_search(search_type="nota", provincia=provincia, tipo_catasto=tipo_catasto or "T", numero_nota=numero_nota)))
    if with_ispezioni and provincia and comune:
        enrichment_steps.append(("ispezioni", f"Ispezioni {provincia}/{comune}",
                                 client.generic_search(search_type="ispezioni", provincia=provincia, comune=comune, tipo_catasto=tipo_catasto or "T", foglio=foglio, particella=particella)))
    if with_ispezioni_cart and provincia and comune:
        enrichment_steps.append(("ispezioni_cartacee", f"Ispezioni cartacee {provincia}/{comune}",
                                 client.generic_search(search_type="ispezioni_cart", provincia=provincia, comune=comune, tipo_catasto=tipo_catasto or "T", foglio=foglio, particella=particella)))

    # Also add soggetto/azienda as enrichment if not already run as starting phase
    if codice_fiscale and not is_person_start:
        enrichment_steps.append(("soggetto", f"Soggetto CF={codice_fiscale}",
                                 client.soggetto(codice_fiscale=codice_fiscale, tipo_catasto=tipo_catasto, provincia=provincia)))
    if azienda_id and not is_company_start:
        enrichment_steps.append(("persona_giuridica", f"Persona giuridica ID={azienda_id}",
                                 client.persona_giuridica(identificativo=azienda_id, tipo_catasto=tipo_catasto, provincia=provincia)))

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
    for key in ("soggetto", "persona_giuridica", "elenco_immobili", "mappa", "fiduciali",
                "originali", "nota", "ispezioni", "ispezioni_cartacee", "indirizzo"):
        if key in all_data and isinstance(all_data[key], dict):
            s = all_data[key].get("status", "?")
            style = "green" if s == "completed" else "red"
            parts.append(f"{key}: [{style}]{s}[/{style}]")
    console.print("  " + "  ".join(parts))

    if output:
        _write_output(all_data, output)


# -- batch command (supports all query types) ---------------------------------

# Maps CSV 'command' column values to (client_method_name, required_fields, extra_field_mapping)
_BATCH_DISPATCHERS = {
    "search": ("search", ("provincia", "comune", "foglio", "particella"), {"tipo_catasto": "tipo_catasto", "subalterno": "subalterno", "sezione": "sezione"}),
    "intestati": ("intestati", ("provincia", "comune", "foglio", "particella", "tipo_catasto"), {"subalterno": "subalterno", "sezione": "sezione"}),
    "soggetto": ("soggetto", ("codice_fiscale",), {"tipo_catasto": "tipo_catasto", "provincia": "provincia"}),
    "azienda": ("persona_giuridica", ("identificativo",), {"tipo_catasto": "tipo_catasto", "provincia": "provincia"}),
    "elenco": ("elenco_immobili", ("provincia", "comune"), {"tipo_catasto": "tipo_catasto", "foglio": "foglio", "sezione": "sezione"}),
    "indirizzo": ("generic_search", ("provincia", "comune", "indirizzo"), {"tipo_catasto": "tipo_catasto"}),
    "partita": ("generic_search", ("provincia", "comune", "partita"), {"tipo_catasto": "tipo_catasto"}),
    "nota": ("generic_search", ("provincia", "numero_nota"), {"anno_nota": "anno_nota", "tipo_catasto": "tipo_catasto"}),
    "mappa": ("generic_search", ("provincia", "comune", "foglio"), {"tipo_catasto": "tipo_catasto"}),
    "ispezioni": ("generic_search", ("provincia", "comune"), {"tipo_catasto": "tipo_catasto", "foglio": "foglio", "particella": "particella"}),
}


@query_app.command()
def batch(
    input_file: str = typer.Option(..., "--input", "-I", help="CSV file with query rows"),
    command: str = typer.Option("search", "--command", "-c", help="Query type: search, intestati, soggetto, azienda, elenco, indirizzo, partita, nota, mappa, ispezioni (or 'auto' to read from CSV 'command' column)"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for each result before submitting the next"),
    output_dir: Optional[str] = typer.Option(None, "--output-dir", "-O", help="Directory — writes one JSON per row"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Single output file (all results merged)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview rows without executing"),
):
    """Submit multiple queries from a CSV file.

    Supports all query types. Use --command to set the type for all rows,
    or add a 'command' column in the CSV for per-row dispatch.

    \b
    Required columns depend on command type:
      search:     provincia, comune, foglio, particella [,tipo_catasto, subalterno]
      intestati:  provincia, comune, foglio, particella, tipo_catasto [,subalterno]
      soggetto:   codice_fiscale [,tipo_catasto, provincia]
      azienda:    identificativo [,tipo_catasto, provincia]
      elenco:     provincia, comune [,tipo_catasto, foglio]
      indirizzo:  provincia, comune, indirizzo [,tipo_catasto]
      partita:    provincia, comune, partita [,tipo_catasto]
      nota:       provincia, numero_nota [,anno_nota, tipo_catasto]
      mappa:      provincia, comune, foglio [,tipo_catasto]
      ispezioni:  provincia, comune [,tipo_catasto, foglio, particella]

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
        dispatcher_info = _BATCH_DISPATCHERS.get(cmd)

        if not dispatcher_info:
            console.print(f"  [red]({i}/{len(rows)}) Unknown command: {cmd}[/red]")
            err_count += 1
            all_results.append({"row": i, "command": cmd, "status": "error", "error": f"Unknown command: {cmd}"})
            continue

        method_name, required_fields, extra_mapping = dispatcher_info
        missing = [f for f in required_fields if f not in row]
        if missing:
            console.print(f"  [red]({i}/{len(rows)}) [{cmd}] Missing fields: {', '.join(missing)}[/red]")
            err_count += 1
            all_results.append({"row": i, "command": cmd, "status": "error", "error": f"Missing: {missing}"})
            continue

        label = f"[{cmd}] " + " ".join(f"{k}={v}" for k, v in list(row.items())[:4])
        console.print(f"\n[dim]({i}/{len(rows)})[/dim] {label}")

        # Build kwargs for the client method
        kwargs = {f: row[f] for f in required_fields}
        for csv_key, method_key in extra_mapping.items():
            if csv_key in row:
                kwargs[method_key] = row[csv_key]

        # For generic_search, inject search_type
        if method_name == "generic_search":
            kwargs["search_type"] = cmd

        try:
            method = getattr(client, method_name)
            result = asyncio.run(method(**kwargs))
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

            entry = {"row": i, "command": cmd, "label": label, "request_ids": request_ids, "results": row_results}
            all_results.append(entry)

            if output_dir:
                row_file = os.path.join(output_dir, f"batch_{i:04d}_{cmd}.json")
                _write_output(entry, row_file)

            if all(r.get("status") == "completed" for r in row_results.values()):
                ok_count += 1
            else:
                err_count += 1
        else:
            all_results.append({"row": i, "command": cmd, "label": label, "request_ids": request_ids, "status": "queued"})
            ok_count += 1

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
        ("query soggetto", "POST", "/visura/soggetto", "National search by codice fiscale"),
        ("query azienda", "POST", "/visura/persona-giuridica", "Search by P.IVA or company name"),
        ("query elenco", "POST", "/visura/elenco-immobili", "List all properties in a comune"),
        ("query indirizzo", "POST", "/visura/indirizzo", "Search by street address"),
        ("query partita", "POST", "/visura/partita", "Search by partita catastale"),
        ("query nota", "POST", "/visura/nota", "Search by annotation/note"),
        ("query mappa", "POST", "/visura/mappa", "View cadastral map data"),
        ("query export-mappa", "POST", "/visura/export-mappa", "Export cadastral map"),
        ("query originali", "POST", "/visura/originali", "Original registration records"),
        ("query fiduciali", "POST", "/visura/fiduciali", "Survey reference points"),
        ("query ispezioni", "POST", "/visura/ispezioni", "Property inspection records"),
        ("query ispezioni-cartacee", "POST", "/visura/ispezioni-cart", "Paper inspection records"),
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
