"""Unit tests for pure Rich renderers used by the SISTER CLI."""

from rich.console import Console
from rich.table import Table

from sister.cli.render import (
    render_health,
    render_history_table,
    render_requests_table,
    render_result,
    render_workflow_result,
    render_workflow_status,
)


def _text(renderable) -> str:
    console = Console(record=True, width=160, force_terminal=False, color_system=None)
    console.print(renderable)
    return console.export_text()


def test_render_result_formats_completed_tables_and_plain_values():
    output = _text(
        render_result(
            {
                "status": "completed",
                "request_id": "req-1",
                "tipo_catasto": "F",
                "timestamp": "2026-11-17",
                "data": {
                    "immobili": [{"Foglio": "5", "Particella": "8", "Sub": "2"}],
                    "intestati": [{"Nominativo": "[red]Ada"}],
                },
            }
        )
    )

    assert "Completata req-1" in output
    assert "Immobili (1)" in output
    assert "Intestati (1)" in output
    assert "[red]Ada" in output


def test_render_result_formats_noncompleted_status_without_markup_interpretation():
    output = _text(render_result({"status": "error", "request_id": "req-2", "error": "[bold]failed"}))

    assert "Richiesta req-2 non riuscita: [bold]failed" in output


def test_render_requests_table_keeps_offset_and_status_columns():
    table = render_requests_table(
        [{"request_id": "req-3", "success": True, "requested_at": "today", "subalterno": None}],
        offset=10,
    )

    assert isinstance(table, Table)
    output = _text(table)
    assert "11" in output
    assert "req-3" in output
    assert "completed" in output
    assert "Inviata" in output


def test_render_history_table_formats_success_and_count():
    output = _text(
        render_history_table(
            [{"request_id": "req-4", "success": False, "created_at": "yesterday"}],
            count=42,
            offset=2,
        )
    )

    assert "Storico visure (42 risultati)" in output
    assert "3" in output
    assert "req-4" in output
    assert "no" in output


def test_render_workflow_status_includes_steps_and_summary():
    output = _text(
        render_workflow_status(
            {
                "status": "running",
                "steps": [{"step": "search", "status": "failed", "error": "not found"}],
                "summary": {"completed": 1, "failed": 1},
            },
            "wf-1",
        )
    )

    assert "Workflow wf-1 — running" in output
    assert "search — failed not found" in output
    assert "completed: 1" in output


def test_render_workflow_result_summarizes_step_data_and_totals():
    output = _text(
        render_workflow_result(
            {
                "steps": [
                    {
                        "step": "search",
                        "status": "completed",
                        "data": {"immobili": [{}, {}], "total": 2, "truncated": True},
                    }
                ],
                "summary": {"completed": 1, "failed": 0, "skipped": 0, "properties": 2, "owners": 1},
            }
        )
    )

    assert "search — completed" in output
    assert "immobili: 2 elementi" in output
    assert "total: 2" in output
    assert "Risultati troncati" in output
    assert "Immobili: 2  Intestatari: 1" in output


def test_render_health_includes_database_metrics_and_service_url():
    output = _text(
        render_health(
            {
                "status": "healthy",
                "authenticated": True,
                "queue_size": 3,
                "database": {"total_requests": 12, "successful": 11},
            },
            "http://localhost:8025",
        )
    )

    assert "Stato API visure" in output
    assert "healthy" in output
    assert "Richieste totali DB" in output
    assert "12" in output
    assert "http://localhost:8025" in output
