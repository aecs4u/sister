"""Small runtime constants and shared CLI plumbing."""

import json
import os
import sys
import types
from collections.abc import Awaitable, Callable
from contextlib import redirect_stdout
from enum import StrEnum
from io import StringIO
from pathlib import Path
from typing import Annotated, Any, Optional, TypeVar

import typer
from rich.console import Console, RenderableType

EXIT_SESSION_EXPIRED = 75  # EX_TEMPFAIL: ask for a fresh SISTER login and resume the batch
console = Console()
T = TypeVar("T")
_json_output = False


class OutputFormat(StrEnum):
    """Supported command output modes."""

    NONE = "none"
    RICH = "rich"
    JSON = "json"


def configure_output(*, json_output: bool) -> None:
    """Route human-readable output to stderr while reserving stdout for JSON."""
    global _json_output
    _json_output = json_output
    console.file = sys.stderr if json_output else sys.stdout
    console.no_color = json_output


def json_output_enabled() -> bool:
    return _json_output


def reset_output() -> None:
    """Restore Rich's dynamic standard-stream lookup after a CLI invocation closes."""
    global _json_output
    _json_output = False
    console.file = None
    console.no_color = None


OutputOpt = Annotated[
    Optional[str], typer.Option("--output", "-o", help="Percorso del file di output (.json)")
]
WaitOpt = Annotated[bool, typer.Option("--wait", "-w", help="Attende il risultato invece di tornare subito")]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="Mostra l'operazione senza inviarla o modificarla")]
ForceOpt = Annotated[bool, typer.Option("--force", help="Ignora la cache e forza una nuova operazione")]
RedownloadOpt = Annotated[
    Optional[bool],
    typer.Option(
        "--yes/--no",
        help="Se la richiesta esiste gia': scarica di nuovo (--yes) o riusa il risultato (--no).",
    ),
]


def project_root() -> Path:
    """Find the SISTER checkout that owns the forwarded scripts and data files."""
    candidates = [os.environ.get("SISTER_HOME"), Path(__file__).resolve().parents[2], Path.cwd()]
    for candidate in candidates:
        if candidate and (Path(candidate) / "scripts").is_dir() and (Path(candidate) / "pyproject.toml").is_file():
            return Path(candidate).resolve()
    console.print(
        "[bold red]Checkout di SISTER non trovato:[/bold red] lancia il comando dalla sua cartella o imposta SISTER_HOME"
    )
    raise typer.Exit(3)


def handle_api_error(exc) -> None:
    """Render a service error and exit without importing the full CLI at module load time."""
    import typer
    from rich.markup import escape

    console.print(f"[bold red]Errore:[/bold red] sister ha restituito HTTP {exc.status_code}: {escape(str(exc.detail))}")
    raise typer.Exit(1)


def emit(
    data: Any,
    *,
    output: str | Path | None = None,
    fmt: OutputFormat = OutputFormat.NONE,
    render: Callable[[Any], RenderableType] | None = None,
) -> None:
    """Write optional JSON output and render data in the requested format."""
    import typer
    from rich.markup import escape

    if output is not None:
        output_path = Path(output)
        content = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            console.print(
                f"[bold red]Impossibile scrivere il file {escape(str(output_path))}:[/bold red] {escape(str(exc))}"
            )
            raise typer.Exit(1) from None
        if fmt is OutputFormat.JSON:
            typer.echo(f"File di output scritto in {output_path}", err=True)
        else:
            console.print(f"[dim]File di output scritto in {escape(str(output_path))}[/dim]")

    if fmt is OutputFormat.JSON:
        typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    elif fmt is OutputFormat.RICH and render is not None:
        console.print(render(data))


def emit_data(data: Any, *, output: str | Path | None = None) -> None:
    """Emit a command result as JSON when requested and optionally save its JSON file."""
    fmt = OutputFormat.JSON if json_output_enabled() else OutputFormat.NONE
    emit(data, output=output, fmt=fmt)


def run_local_tool(command: str, operation: Callable[[], T]) -> T:
    """Run an in-process tool and keep its report out of stdout in JSON mode."""
    import typer

    if not json_output_enabled():
        return operation()

    captured = StringIO()
    redirected_consoles: list[tuple[Console, Any, Any]] = []
    redirected_console_ids: set[int] = set()
    for module_name, module in tuple(sys.modules.items()):
        if not module_name.startswith("sister.command_tools.") or not isinstance(module, types.ModuleType):
            continue
        for value in vars(module).values():
            if isinstance(value, Console) and value is not console and id(value) not in redirected_console_ids:
                redirected_console_ids.add(id(value))
                redirected_consoles.append((value, value.file, value.no_color))
                value.file = captured
                value.no_color = True

    def publish(result: Any = None, *, error: Exception | None = None, exit_code: int | None = None) -> None:
        output = captured.getvalue()
        if output:
            typer.echo(output.rstrip("\n"), err=True)
        payload: dict[str, Any] = {"command": command, "exit_code": exit_code or 0}
        if output:
            payload["output"] = output
        if result is not None:
            payload["result"] = result
        if error is not None and not isinstance(error, typer.Exit):
            payload["error"] = str(error) or error.__class__.__name__
        emit_data(payload)

    try:
        with redirect_stdout(captured):
            result = operation()
    except typer.Exit as exc:
        publish(error=exc, exit_code=exc.exit_code)
        raise
    except KeyboardInterrupt:
        publish(error=KeyboardInterrupt(), exit_code=130)
        raise typer.Exit(130) from None
    except Exception as exc:
        publish(error=exc, exit_code=1)
        raise
    finally:
        for tool_console, output_file, no_color in redirected_consoles:
            tool_console.file = output_file
            tool_console.no_color = no_color

    if isinstance(result, int):
        publish(exit_code=result)
    else:
        publish(result=result)
    return result


def exit_for_status(*results: dict) -> None:
    """Exit with status 1 when any service result reports an unsuccessful terminal state."""
    import typer

    if any(result.get("status") in {"error", "expired", "needs_human"} for result in results if isinstance(result, dict)):
        raise typer.Exit(1)


def run_api(coro_factory: Callable[[Any], Awaitable[T]], *, client: Any | None = None) -> T:
    """Run one API operation and its follow-up awaits in a single managed event loop."""
    import asyncio

    import httpx
    import typer

    from ..client import VisuraAPIError, VisuraClient

    active_client = client or VisuraClient()

    async def _run() -> T:
        async with active_client:
            return await coro_factory(active_client)

    try:
        return asyncio.run(_run())
    except VisuraAPIError as exc:
        handle_api_error(exc)
    except httpx.ConnectError as exc:
        from rich.markup import escape

        console.print(
            f"[bold red]Servizio non raggiungibile all'indirizzo {active_client.base_url} "
            f"(avviare l'app sister?): {escape(str(exc))}[/bold red]"
        )
        raise typer.Exit(3) from None
    except KeyboardInterrupt:
        raise typer.Exit(130) from None


def run_db(coro_factory: Callable[[], Awaitable[T]], *, initialize: bool = True) -> T:
    """Run a database command in one managed event loop, validating the schema by default."""
    import asyncio

    import typer

    from .. import database

    async def _run() -> T:
        if initialize:
            await database.init_db()
        return await coro_factory()

    try:
        return asyncio.run(_run())
    except RuntimeError as exc:
        message = str(exc)
        if message.startswith("SISTER_DATABASE_DSN_") and "must be set to a PostgreSQL connection URL" in message:
            console.print(f"[bold red]Database non configurato:[/bold red] {message}")
            raise typer.Exit(3) from None
        raise
