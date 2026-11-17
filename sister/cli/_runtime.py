"""Small runtime constants and shared CLI plumbing."""

import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

from rich.console import Console

EXIT_SESSION_EXPIRED = 75  # EX_TEMPFAIL: ask for a fresh SISTER login and resume the batch
console = Console()
T = TypeVar("T")


def project_root() -> Path:
    """Find the SISTER checkout that owns the forwarded scripts and data files."""
    candidates = [os.environ.get("SISTER_HOME"), Path(__file__).resolve().parents[2], Path.cwd()]
    for candidate in candidates:
        if candidate and (Path(candidate) / "scripts").is_dir() and (Path(candidate) / "pyproject.toml").is_file():
            return Path(candidate).resolve()
    raise FileNotFoundError(
        "checkout di SISTER non trovato: lancia il comando dalla sua cartella o imposta SISTER_HOME"
    )


def handle_api_error(exc) -> None:
    """Render a service error and exit without importing the full CLI at module load time."""
    import typer
    from rich.markup import escape

    console.print(f"[bold red]Error:[/bold red] sister returned HTTP {exc.status_code}: {escape(str(exc.detail))}")
    raise typer.Exit(1)


def write_output(data: dict | list, path: str | Path) -> None:
    """Write JSON output, creating parent directories when needed."""
    import typer
    from rich.markup import escape

    output_path = Path(path)
    content = json.dumps(data, indent=2, ensure_ascii=False)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        console.print(f"[bold red]Cannot write output to {escape(str(output_path))}:[/bold red] {escape(str(exc))}")
        raise typer.Exit(1) from None
    console.print(f"[dim]Output written to {escape(str(output_path))}[/dim]")


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
            f"[bold red]Service not reachable at {active_client.base_url} "
            f"(sister app start?): {escape(str(exc))}[/bold red]"
        )
        raise typer.Exit(3) from None
    except KeyboardInterrupt:
        raise typer.Exit(130) from None


def run_db(coro_factory: Callable[[], Awaitable[T]]) -> T:
    """Initialize the selected SISTER database and run one command in a managed event loop."""
    import asyncio

    import typer

    from .. import database

    async def _run() -> T:
        await database.init_db()
        return await coro_factory()

    try:
        return asyncio.run(_run())
    except RuntimeError as exc:
        message = str(exc)
        if message.startswith("SISTER_DATABASE_DSN_") and "must be set to a PostgreSQL connection URL" in message:
            console.print(f"[bold red]Database not configured:[/bold red] {message}")
            raise typer.Exit(3) from None
        raise
