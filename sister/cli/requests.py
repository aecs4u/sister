"""Legacy root-level request commands, retained until the command-tree migration."""

import time
from typing import Optional

import typer
from rich.table import Table

from . import _runtime
from ._runtime import console
from .render import _print_result

_write_output = _runtime.write_output


def get_result(
    request_id: str = typer.Argument(help="ID della richiesta da recuperare"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Percorso del file di output (.json)"),
):
    """Recupera il risultato di una richiesta di visura tramite ID (GET /api/v1/visura/{request_id}).

    Returns the current status: processing, completed, error, or expired.
    """
    result = _runtime.run_api(lambda client: client.get_result(request_id))

    _print_result(result)

    if output:
        _write_output(result, output)

    if result.get("status") in {"error", "expired", "needs_human"}:
        raise typer.Exit(1)


def wait_cmd(
    request_id: str = typer.Argument(help="ID della richiesta da attendere"),
    timeout: Optional[float] = typer.Option(
        None, "--timeout", "-T",
        help="Secondi massimi di attesa (predefinito: da env)",
    ),
    interval: Optional[float] = typer.Option(
        None, "--interval",
        help="Secondi tra un controllo e l'altro (predefinito: da env)",
    ),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Percorso del file di output (.json)"),
):
    """Controlla una richiesta finché non termina o scade il tempo.

    Continuously polls GET /api/v1/visura/{request_id} until status is
    'completed' or 'error', then prints the result.
    """
    start = time.monotonic()

    console.print(f"[dim]Waiting for {request_id}...[/dim]")

    try:
        result = _runtime.run_api(
            lambda client: client.wait_for_result(request_id, poll_interval=interval, poll_timeout=timeout)
        )
    except TimeoutError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1) from None

    elapsed = time.monotonic() - start
    status = result.get("status", "unknown")
    if status == "completed":
        console.print(f"[dim]Completed in {elapsed:.1f}s[/dim]")
    else:
        console.print(f"[dim]Finished in {elapsed:.1f}s (status: {status})[/dim]")
    _print_result(result)

    if output:
        _write_output(result, output)

    if status in {"error", "expired", "needs_human"}:
        raise typer.Exit(1)


def requests(
    provincia: Optional[str] = typer.Option(None, "--provincia", "-P", help="Filtra per provincia"),
    comune: Optional[str] = typer.Option(None, "--comune", "-C", help="Filtra per comune"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Filtra per foglio"),
    particella: Optional[str] = typer.Option(None, "--particella", "-p", help="Filtra per particella"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="Filtra per tipo (T/F)"),
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filtro: completed, pending, failed"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=200, help="Numero massimo di risultati (1–200)"),
    offset: int = typer.Option(0, "--offset", min=0, help="Offset per la paginazione"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Percorso del file di output (.json)"),
):
    """Elenca tutte le richieste e il loro stato dal database.

    Shows both requests that have a response (completed/failed) and those
    still pending. Use --status to filter.
    """
    _VALID_STATUSES = {"completed", "pending", "failed"}
    if status and status.lower() not in _VALID_STATUSES:
        console.print(f"[red]Invalid --status '{status}'. Must be one of: {', '.join(sorted(_VALID_STATUSES))}[/red]")
        raise typer.Exit(1)
    if status:
        status = status.lower()

    async def fetch_requests(client):
        filters = {
            "provincia": provincia,
            "comune": comune,
            "foglio": foglio,
            "particella": particella,
            "tipo_catasto": tipo_catasto,
        }
        result = await client.history(**filters, limit=limit, offset=offset, status=status)
        rows = result.get("results", [])
        if not status:
            return rows
        # the service filters in SQL; the same test here keeps an older service (which ignores ``status``) correct
        return [
            row
            for row in rows
            if (status == "completed" and row.get("success") is True)
            or (status == "failed" and row.get("success") is False)
            or (status == "pending" and row.get("responded_at") is None)
        ]

    try:
        items = _runtime.run_api(fetch_requests)
    except TimeoutError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1) from None

    if not items:
        console.print("[dim]No requests found.[/dim]")
        if output:
            _write_output({"count": 0, "results": []}, output)
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


def history(
    provincia: Optional[str] = typer.Option(None, "--provincia", "-P", help="Filtra per provincia"),
    comune: Optional[str] = typer.Option(None, "--comune", "-C", help="Filtra per comune"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Filtra per foglio"),
    particella: Optional[str] = typer.Option(None, "--particella", "-p", help="Filtra per particella"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="Filtra per tipo (T/F)"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=200, help="Numero massimo di risultati (1–200)"),
    offset: int = typer.Option(0, "--offset", min=0, help="Offset per la paginazione"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Percorso del file di output (.json)"),
):
    """Interroga lo storico delle risposte di visura nel database."""
    try:
        result = _runtime.run_api(
            lambda client: client.history(
                provincia=provincia,
                comune=comune,
                foglio=foglio,
                particella=particella,
                tipo_catasto=tipo_catasto,
                limit=limit,
                offset=offset,
            )
        )
    except TimeoutError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1) from None

    items = result.get("results", [])
    count = result.get("count", len(items))

    if not items:
        console.print("[dim]No history records found.[/dim]")
        if output:
            _write_output(result, output)
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


def register_legacy_request_commands(app: typer.Typer) -> None:
    """Mount the original root-level command names without changing the public CLI tree."""
    app.command("get")(get_result)
    app.command("wait")(wait_cmd)
    app.command()(requests)
    app.command()(history)
