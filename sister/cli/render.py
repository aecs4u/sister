"""Pure Rich renderers for SISTER CLI response data."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from rich.console import Group, RenderableType
from rich.rule import Rule
from rich.table import Table
from rich.text import Text


def _cell(value: Any, default: str = "-") -> Text:
    """Make an unstyled cell so provider text is never interpreted as Rich markup."""
    return Text(default if value is None else str(value))


def _records_table(
    title: str,
    rows: Sequence[Any],
    *,
    no_wrap: Sequence[str] = (),
) -> Table | None:
    records = [row for row in rows if isinstance(row, Mapping)]
    if not records:
        return None

    columns = list(records[0])
    if not columns:
        return None

    table = Table(title=title, header_style="bold cyan")
    for column in columns:
        table.add_column(str(column), no_wrap=column in no_wrap)
    for row in records:
        table.add_row(*(_cell(row.get(column), "") for column in columns))
    return table


def render_result(result: dict) -> RenderableType:
    """Build the status-aware display for one visura result without writing to a console."""
    status = result.get("status", "unknown")
    request_id = str(result.get("request_id", "?"))

    if status == "processing":
        return Group(
            Text(f"La richiesta {request_id} è ancora in elaborazione.", style="yellow"),
            Text(f"Ripeti più tardi oppure usa: sister requests wait {request_id}", style="dim"),
        )
    if status == "expired":
        return Text(f"La richiesta {request_id} è scaduta (risultato rimosso dalla cache).", style="red")
    if status == "error":
        error = str(result.get("error", "unknown error"))
        line = Text(f"Richiesta {request_id} non riuscita:", style="red")
        line.append(f" {error}")
        return line
    if status == "needs_human":
        line = Text(f"La richiesta {request_id} richiede un intervento:", style="yellow")
        line.append(" CAPTCHA SISTER non risolto: non è stato richiesto alcun documento.")
        return line

    if status == "completed":
        tipo = result.get("tipo_catasto", "")
        timestamp = result.get("timestamp", "")
        header = Text("Completata", style="bold green")
        header.append(f" {request_id}")
        if tipo:
            header.append(f" ({tipo})", style="dim")
        if timestamp:
            header.append(f"  {timestamp}", style="dim")

        data = result.get("data", {})
        data = data if isinstance(data, dict) else {}
        parts: list[RenderableType] = [header]
        cost = data.get("cost")
        if isinstance(cost, Mapping):
            cost_value = cost.get("value", 0)
            try:
                cost_value = float(cost_value)
            except (TypeError, ValueError):
                cost_value = 0.0
            parts.append(Text(f"Costo: {cost.get('text', 'N/D')} (€{cost_value:.2f})", style="yellow"))
            if not data.get("confirmed"):
                parts.append(Text("Costo non confermato. Prosegui dalla pagina admin per le operazioni a pagamento.", style="yellow"))
        immobili = data.get("immobili", [])
        intestati = data.get("intestati", [])
        immobili_rows = immobili if isinstance(immobili, list) else []
        intestati_rows = intestati if isinstance(intestati, list) else []

        immobili_table = _records_table(
            f"Immobili ({len(immobili_rows)})",
            immobili_rows,
            no_wrap=("Foglio", "Particella", "Sub"),
        )
        intestati_table = _records_table(
            f"Intestati ({len(intestati_rows)})",
            intestati_rows,
        )
        if immobili_table:
            parts.append(immobili_table)
        if intestati_table:
            parts.append(intestati_table)
        if not immobili_table and not intestati_table and data:
            parts.append(Text(json.dumps(data, indent=2, ensure_ascii=False)))
        return Group(*parts)

    return Text(json.dumps(result, indent=2, ensure_ascii=False))


def render_requests_table(items: Sequence[Mapping[str, Any]], offset: int = 0) -> Table:
    """Build the legacy requests list table."""
    table = Table(title=f"Richieste ({len(items)} visualizzate)", header_style="bold cyan")
    table.add_column("#", style="dim", justify="right", no_wrap=True)
    table.add_column("ID richiesta", style="green", no_wrap=True)
    table.add_column("Tipo", style="cyan", no_wrap=True)
    table.add_column("Cat.", style="cyan", no_wrap=True)
    table.add_column("Provincia")
    table.add_column("Comune")
    table.add_column("F.", no_wrap=True)
    table.add_column("P.", no_wrap=True)
    table.add_column("Sub.", no_wrap=True)
    table.add_column("Stato", no_wrap=True)
    table.add_column("Inviata", style="dim", no_wrap=True)

    for index, row in enumerate(items, 1 + offset):
        success = row.get("success")
        responded = row.get("responded_at")
        if success is True:
            status = Text("completed", style="green")
        elif success is False:
            status = Text("failed", style="red")
        elif responded is None:
            status = Text("pending", style="yellow")
        else:
            status = Text("-")

        table.add_row(
            str(index),
            _cell(row.get("request_id")),
            _cell(row.get("request_type")),
            _cell(row.get("tipo_catasto")),
            _cell(row.get("provincia")),
            _cell(row.get("comune")),
            _cell(row.get("foglio")),
            _cell(row.get("particella")),
            _cell(row.get("subalterno") or "-"),
            status,
            _cell(row.get("requested_at", row.get("created_at"))),
        )
    return table


def render_history_table(items: Sequence[Mapping[str, Any]], count: int, offset: int = 0) -> Table:
    """Build the legacy visura history table."""
    table = Table(title=f"Storico visure ({count} risultati)", header_style="bold cyan")
    table.add_column("#", style="dim", justify="right", no_wrap=True)
    table.add_column("ID richiesta", style="green", no_wrap=True)
    table.add_column("Cat.", style="cyan", no_wrap=True)
    table.add_column("Provincia", style="white")
    table.add_column("Comune", style="white")
    table.add_column("Foglio", style="white", no_wrap=True)
    table.add_column("Particella", style="white", no_wrap=True)
    table.add_column("Esito", style="yellow", no_wrap=True)
    table.add_column("Creata", style="dim", no_wrap=True)

    for index, row in enumerate(items, 1 + offset):
        success = row.get("success")
        if success is True:
            success_cell = Text("yes", style="green")
        elif success is False:
            success_cell = Text("no", style="red")
        else:
            success_cell = Text("-")

        table.add_row(
            str(index),
            _cell(row.get("request_id")),
            _cell(row.get("tipo_catasto")),
            _cell(row.get("provincia")),
            _cell(row.get("comune")),
            _cell(row.get("foglio")),
            _cell(row.get("particella")),
            success_cell,
            _cell(row.get("requested_at", row.get("created_at"))),
        )
    return table


def render_workflow_status(result: dict, workflow_id: str) -> RenderableType:
    """Build the server workflow status display without printing it."""
    steps = result.get("steps") or []
    summary = result.get("summary") or {}
    if not steps and not summary:
        return Text(json.dumps(result, indent=2, ensure_ascii=False))

    heading = Text("Workflow ", style="bold")
    heading.append(str(workflow_id), style="bold")
    heading.append(f" — {result.get('status', 'unknown')}")
    parts: list[RenderableType] = [heading]
    for step in steps:
        status = step.get("status", "unknown")
        style = "green" if status == "completed" else "red" if status in {"error", "failed"} else "yellow"
        line = Text("  ")
        line.append(str(step.get("step", "?")), style=style)
        line.append(f" — {status}")
        if step.get("error"):
            line.append(f" {step['error']}")
        parts.append(line)
    if summary:
        parts.append(Text("  " + "  ".join(f"{key}: {value}" for key, value in summary.items())))
    return Group(*parts)


def render_workflow_result(result: dict) -> RenderableType:
    """Build the result summary for a completed preset workflow."""
    parts: list[RenderableType] = []
    for step in result.get("steps", []):
        status = step.get("status", "unknown")
        style = "green" if status == "completed" else "red" if status == "error" else "yellow"
        line = Text("\n  ")
        line.append(str(step.get("step", "?")), style=style)
        line.append(f" — {status}")
        if status == "error":
            line.append(f"\n    {step.get('error', '')}", style="red")
        elif status == "completed" and isinstance(step.get("data"), dict):
            data = step["data"]
            for key in ("immobili", "intestati", "risultati", "drill_results"):
                items = data.get(key, [])
                if isinstance(items, list) and items:
                    line.append(f"\n    {key}: {len(items)} elementi")
            if "total" in data:
                line.append(f"\n    total: {data['total']}")
            if data.get("truncated"):
                line.append("\n    Risultati troncati (massimo 20 immobili di approfondimento)", style="yellow")
        parts.append(line)

    parts.append(Rule("Workflow completato", style="bold green"))
    summary = result.get("summary") or {}
    steps_line = Text("  Passi: ")
    steps_line.append(str(summary.get("completed", 0)), style="green")
    steps_line.append(" completati, ")
    steps_line.append(str(summary.get("failed", 0)), style="red")
    steps_line.append(" non riusciti, ")
    steps_line.append(str(summary.get("skipped", 0)), style="yellow")
    steps_line.append(" saltati")
    parts.append(steps_line)

    if summary.get("properties", 0) > 0:
        parts.append(
            Text(f"  Immobili: {summary['properties']}  Intestatari: {summary.get('owners', 0)}", style="cyan")
        )
    if summary.get("risk_flags", 0) > 0:
        parts.append(Text(f"  Indicatori di rischio: {summary['risk_flags']}", style="yellow"))
    return Group(*parts)


def render_health(result: dict, service_url: str) -> Group:
    """Build the service health table and URL line without printing them."""
    status = result.get("status", "unknown")
    authenticated = result.get("authenticated", False)
    db_stats = result.get("database", {})

    table = Table(title="Stato API visure", header_style="bold cyan")
    table.add_column("Metrica", style="cyan", no_wrap=True)
    table.add_column("Valore", style="white")
    table.add_row("Stato", Text(str(status), style="green" if status == "healthy" else "red"))
    table.add_row("Autenticato", Text(str(authenticated), style="green" if authenticated else "red"))
    table.add_row("Dimensione coda", _cell(result.get("queue_size", "?")))
    table.add_row("Richieste in attesa", _cell(result.get("pending_requests", "?")))
    table.add_row("Risposte in cache", _cell(result.get("cached_responses", "?")))
    table.add_row("Dimensione massima coda", _cell(result.get("queue_max_size", "?")))
    table.add_row("Durata cache risposte", Text(f"{result.get('response_ttl_seconds', '?')}s"))

    if db_stats:
        table.add_section()
        table.add_row("Richieste totali DB", _cell(db_stats.get("total_requests", "?")))
        table.add_row("Risposte totali DB", _cell(db_stats.get("total_responses", "?")))
        table.add_row("Operazioni riuscite DB", _cell(db_stats.get("successful", "?")))
        table.add_row("Operazioni non riuscite DB", _cell(db_stats.get("failed", "?")))

    return Group(table, Text(f"URL del servizio: {service_url}", style="dim"))
