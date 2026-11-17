"""Request lookup, polling and history commands."""

import inspect
import time
from typing import Optional

import typer

from . import _runtime
from ._runtime import OutputOpt, console
from .render import render_history_table, render_requests_table, render_result

requests_app = typer.Typer(
    name="requests",
    help="Recupera, attende ed elenca richieste di visura.",
    invoke_without_command=True,
    no_args_is_help=False,
)


def _get_result(request_id: str, output: Optional[str] = None) -> None:
    result = _runtime.run_api(lambda client: client.get_result(request_id))
    console.print(render_result(result))
    _runtime.emit_data(result, output=output)
    _runtime.exit_for_status(result)


def _wait_for_result(
    request_id: str,
    timeout: Optional[float] = None,
    interval: Optional[float] = None,
    output: Optional[str] = None,
) -> None:
    start = time.monotonic()
    console.print(f"[dim]In attesa della richiesta {request_id}...[/dim]")
    try:
        result = _runtime.run_api(
            lambda client: client.wait_for_result(request_id, poll_interval=interval, poll_timeout=timeout)
        )
    except TimeoutError as exc:
        console.print(f"[bold red]Errore:[/bold red] {exc}")
        raise typer.Exit(1) from None

    elapsed = time.monotonic() - start
    status = result.get("status", "unknown")
    label = "Completata" if status == "completed" else f"Terminata (stato: {status})"
    console.print(f"[dim]{label} in {elapsed:.1f}s[/dim]")
    console.print(render_result(result))
    _runtime.emit_data(result, output=output)
    _runtime.exit_for_status(result)


def _list_requests(
    provincia: Optional[str] = None,
    comune: Optional[str] = None,
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    output: Optional[str] = None,
) -> None:
    valid_statuses = {"completed", "pending", "failed"}
    if status and status.casefold() not in valid_statuses:
        raise typer.BadParameter("--status deve essere completed, pending oppure failed")
    status = status.casefold() if status else None

    async def fetch_requests(client):
        result = await client.history(
            provincia=provincia,
            comune=comune,
            foglio=foglio,
            particella=particella,
            tipo_catasto=tipo_catasto,
            limit=limit,
            offset=offset,
            status=status,
        )
        return result.get("results", [])

    try:
        items = _runtime.run_api(fetch_requests)
    except TimeoutError as exc:
        console.print(f"[bold red]Errore:[/bold red] {exc}")
        raise typer.Exit(1) from None

    payload = {"count": len(items), "results": items}
    if not items:
        console.print("[dim]Nessuna richiesta trovata.[/dim]")
        _runtime.emit_data(payload, output=output)
        return

    console.print(render_requests_table(items, offset))
    _runtime.emit_data(payload, output=output)


def _history(
    provincia: Optional[str] = None,
    comune: Optional[str] = None,
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    output: Optional[str] = None,
) -> None:
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
        console.print(f"[bold red]Errore:[/bold red] {exc}")
        raise typer.Exit(1) from None

    items = result.get("results", [])
    count = result.get("count", len(items))
    if not items:
        console.print("[dim]Nessun record nello storico.[/dim]")
        _runtime.emit_data(result, output=output)
        return

    console.print(render_history_table(items, count, offset))
    _runtime.emit_data(result, output=output)


def _legacy_history(
    provincia: Optional[str] = typer.Option(None, "--provincia", "-P", help="Filtra per provincia"),
    comune: Optional[str] = typer.Option(None, "--comune", "-C", help="Filtra per comune"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Filtra per foglio"),
    particella: Optional[str] = typer.Option(None, "--particella", "-p", help="Filtra per particella"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="Filtra per tipo (T/F)"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=200, help="Numero massimo di risultati"),
    offset: int = typer.Option(0, "--offset", min=0, help="Offset per la paginazione"),
    output: OutputOpt = None,
):
    return _history(provincia, comune, foglio, particella, tipo_catasto, limit, offset, output)


@requests_app.callback()
def requests_group(
    ctx: typer.Context,
    provincia: Optional[str] = typer.Option(None, "--provincia", "-P", help="Filtra per provincia"),
    comune: Optional[str] = typer.Option(None, "--comune", "-C", help="Filtra per comune"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Filtra per foglio"),
    particella: Optional[str] = typer.Option(None, "--particella", "-p", help="Filtra per particella"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="Filtra per tipo (T/F)"),
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filtro: completed, pending, failed"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=200, help="Numero massimo di risultati"),
    offset: int = typer.Option(0, "--offset", min=0, help="Offset per la paginazione"),
    output: OutputOpt = None,
):
    """Senza sottocomando, mantiene temporaneamente il vecchio `sister requests --status ...`."""
    if ctx.invoked_subcommand is None:
        typer.echo("I filtri di `sister requests` sono deprecati; usa `sister requests list`.", err=True)
        _list_requests(provincia, comune, foglio, particella, tipo_catasto, status, limit, offset, output)


@requests_app.command("get")
def request_get(
    request_id: str = typer.Argument(help="ID della richiesta da recuperare"),
    output: OutputOpt = None,
):
    """Recupera il risultato di una richiesta tramite ID."""
    _get_result(request_id, output)


@requests_app.command("wait")
def request_wait(
    request_id: str = typer.Argument(help="ID della richiesta da attendere"),
    timeout: Optional[float] = typer.Option(None, "--timeout", "-T", help="Secondi massimi di attesa"),
    interval: Optional[float] = typer.Option(None, "--interval", help="Secondi tra i controlli"),
    output: OutputOpt = None,
):
    """Attende il completamento o la scadenza della richiesta."""
    _wait_for_result(request_id, timeout, interval, output)


@requests_app.command("list")
def requests_list(
    provincia: Optional[str] = typer.Option(None, "--provincia", "-P", help="Filtra per provincia"),
    comune: Optional[str] = typer.Option(None, "--comune", "-C", help="Filtra per comune"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Filtra per foglio"),
    particella: Optional[str] = typer.Option(None, "--particella", "-p", help="Filtra per particella"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="Filtra per tipo (T/F)"),
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filtro: completed, pending, failed"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=200, help="Numero massimo di risultati"),
    offset: int = typer.Option(0, "--offset", min=0, help="Offset per la paginazione"),
    output: OutputOpt = None,
):
    """Elenca richieste e risposte con filtri catastali e di stato."""
    _list_requests(provincia, comune, foglio, particella, tipo_catasto, status, limit, offset, output)


def _deprecated_alias(alias: str, target, new_command: str):
    """Build a hidden root alias that warns and forwards to a grouped command."""

    def command(**kwargs):
        typer.echo(f"`sister {alias}` è deprecato; usa `sister {new_command}`.", err=True)
        return target(**kwargs)

    command.__signature__ = inspect.signature(target)
    command.__annotations__ = dict(getattr(target, "__annotations__", {}))
    command.__name__ = f"deprecated_{alias.replace('-', '_')}"
    command.__doc__ = f"Deprecated alias for `sister requests {target.__name__}`."
    return command


def register_request_commands(app: typer.Typer) -> None:
    """Mount the grouped request interface and one-release root aliases."""
    app.add_typer(requests_app, name="requests", rich_help_panel="Service")
    app.command("get", hidden=True)(_deprecated_alias("get", request_get, "requests get"))
    app.command("wait", hidden=True)(_deprecated_alias("wait", request_wait, "requests wait"))
    app.command("history", hidden=True)(_deprecated_alias("history", _legacy_history, "requests list"))


request_get.endpoint = "/api/v1/visura/{request_id}"
request_get.method = "GET"
request_wait.endpoint = "/api/v1/visura/{request_id}"
request_wait.method = "GET"
requests_list.endpoint = "/api/v1/visura/history"
requests_list.method = "GET"
