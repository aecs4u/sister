"""Rich renderers for SISTER CLI response data."""

import json

from rich.table import Table

from ._runtime import console


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
