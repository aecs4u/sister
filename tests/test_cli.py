"""Tests for the CLI commands (cli.py).

Uses Typer's CliRunner to invoke commands without a real server.
API calls are mocked via monkeypatch on the VisuraClient methods.
"""

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sister.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _patch_client(monkeypatch, method_name, return_value=None, side_effect=None):
    """Patch a VisuraClient async method to return a canned value."""
    from sister.client import VisuraClient

    if side_effect:
        async def fake(*args, **kwargs):
            raise side_effect
    else:
        async def fake(*args, **kwargs):
            return return_value

    monkeypatch.setattr(VisuraClient, method_name, fake)


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


def test_queries_lists_endpoints():
    result = runner.invoke(app, ["queries"])
    assert result.exit_code == 0
    assert "/visura" in result.output
    assert "/health" in result.output
    assert "search" in result.output
    assert "intestati" in result.output


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_dry_run():
    result = runner.invoke(app, [
        "query", "search", "-P", "Trieste", "-C", "TRIESTE", "-F", "9", "-p", "166",
        "--dry-run",
    ])
    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert "POST" in result.output
    assert "Trieste" in result.output


def test_search_submits_and_prints_ids(monkeypatch):
    _patch_client(monkeypatch, "search", {
        "request_ids": ["req_T_001", "req_F_002"],
        "status": "queued",
    })

    result = runner.invoke(app, [
        "query", "search", "-P", "Trieste", "-C", "TRIESTE", "-F", "9", "-p", "166",
    ])
    assert result.exit_code == 0
    assert "req_T_001" in result.output
    assert "req_F_002" in result.output
    assert "queued" in result.output


def test_search_with_tipo_catasto(monkeypatch):
    _patch_client(monkeypatch, "search", {
        "request_ids": ["req_F_only"],
        "status": "queued",
    })

    result = runner.invoke(app, [
        "query", "search", "-P", "Roma", "-C", "ROMA", "-F", "1", "-p", "1", "-t", "F",
    ])
    assert result.exit_code == 0
    assert "req_F_only" in result.output


def test_search_handles_api_error(monkeypatch):
    from sister.client import VisuraAPIError

    _patch_client(monkeypatch, "search", side_effect=VisuraAPIError(503, "Service down"))

    result = runner.invoke(app, [
        "query", "search", "-P", "X", "-C", "X", "-F", "1", "-p", "1",
    ])
    assert result.exit_code == 1
    assert "503" in result.output


def test_search_missing_required_option():
    result = runner.invoke(app, ["query", "search", "-P", "Trieste"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# intestati
# ---------------------------------------------------------------------------


def test_intestati_dry_run():
    result = runner.invoke(app, [
        "query", "intestati", "-P", "Trieste", "-C", "TRIESTE", "-F", "9", "-p", "166",
        "-t", "F", "-sub", "3", "--dry-run",
    ])
    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert "/visura/intestati" in result.output


def test_intestati_submits_and_prints_id(monkeypatch):
    _patch_client(monkeypatch, "intestati", {
        "request_id": "intestati_F_abc",
        "status": "queued",
    })

    result = runner.invoke(app, [
        "query", "intestati", "-P", "Trieste", "-C", "TRIESTE", "-F", "9", "-p", "166",
        "-t", "F", "-sub", "3",
    ])
    assert result.exit_code == 0
    assert "intestati_F_abc" in result.output
    assert "queued" in result.output


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_completed(monkeypatch):
    _patch_client(monkeypatch, "get_result", {
        "request_id": "req_F_done",
        "status": "completed",
        "tipo_catasto": "F",
        "data": {"immobili": [{"Foglio": "9", "Particella": "166"}]},
        "timestamp": "2025-01-01T12:00:00",
    })

    result = runner.invoke(app, ["get", "req_F_done"])
    assert result.exit_code == 0
    assert "Completed" in result.output
    assert "req_F_done" in result.output


def test_get_processing(monkeypatch):
    _patch_client(monkeypatch, "get_result", {
        "request_id": "req_F_proc",
        "status": "processing",
    })

    result = runner.invoke(app, ["get", "req_F_proc"])
    assert result.exit_code == 0
    assert "processing" in result.output


def test_get_expired(monkeypatch):
    _patch_client(monkeypatch, "get_result", {
        "request_id": "req_F_exp",
        "status": "expired",
    })

    result = runner.invoke(app, ["get", "req_F_exp"])
    assert result.exit_code == 0
    assert "expired" in result.output


def test_get_error_status(monkeypatch):
    _patch_client(monkeypatch, "get_result", {
        "request_id": "req_F_err",
        "status": "error",
        "error": "Session lost",
    })

    result = runner.invoke(app, ["get", "req_F_err"])
    assert result.exit_code == 0
    assert "Session lost" in result.output


def test_get_writes_output(monkeypatch, tmp_path):
    _patch_client(monkeypatch, "get_result", {
        "request_id": "req_F_out",
        "status": "completed",
        "data": {"ok": True},
    })

    out_file = tmp_path / "result.json"
    result = runner.invoke(app, ["get", "req_F_out", "-o", str(out_file)])
    assert result.exit_code == 0

    written = json.loads(out_file.read_text())
    assert written["request_id"] == "req_F_out"


def test_get_404(monkeypatch):
    from sister.client import VisuraAPIError

    _patch_client(monkeypatch, "get_result", side_effect=VisuraAPIError(404, "Not found"))

    result = runner.invoke(app, ["get", "nonexistent"])
    assert result.exit_code == 1
    assert "404" in result.output


# ---------------------------------------------------------------------------
# wait
# ---------------------------------------------------------------------------


def test_wait_completes(monkeypatch):
    _patch_client(monkeypatch, "wait_for_result", {
        "request_id": "req_wait",
        "status": "completed",
        "data": {"immobili": []},
    })

    result = runner.invoke(app, ["wait", "req_wait"])
    assert result.exit_code == 0
    assert "Completed" in result.output


def test_wait_timeout(monkeypatch):
    _patch_client(
        monkeypatch, "wait_for_result",
        side_effect=TimeoutError("Timed out after 10s waiting for req_slow"),
    )

    result = runner.invoke(app, ["wait", "req_slow"])
    assert result.exit_code == 1
    assert "Timed out" in result.output


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def test_history_empty(monkeypatch):
    _patch_client(monkeypatch, "history", {"results": [], "count": 0})

    result = runner.invoke(app, ["history"])
    assert result.exit_code == 0
    assert "No history records found" in result.output


def test_history_with_results(monkeypatch):
    _patch_client(monkeypatch, "history", {
        "results": [
            {
                "request_id": "r1",
                "tipo_catasto": "T",
                "provincia": "Trieste",
                "comune": "TRIESTE",
                "foglio": "9",
                "particella": "166",
                "success": True,
                "requested_at": "2025-01-01T12:00:00",
            },
        ],
        "count": 1,
    })

    result = runner.invoke(app, ["history", "-P", "Trieste"])
    assert result.exit_code == 0
    assert "r1" in result.output
    assert "1 results" in result.output


def test_history_with_output(monkeypatch, tmp_path):
    _patch_client(monkeypatch, "history", {
        "results": [{"request_id": "r1"}],
        "count": 1,
    })

    out_file = tmp_path / "history.json"
    result = runner.invoke(app, ["history", "-o", str(out_file)])
    assert result.exit_code == 0

    written = json.loads(out_file.read_text())
    assert written["count"] == 1


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def test_health_healthy(monkeypatch):
    _patch_client(monkeypatch, "health", {
        "status": "healthy",
        "authenticated": True,
        "queue_size": 0,
        "pending_requests": 0,
        "cached_responses": 5,
        "queue_max_size": 100,
        "response_ttl_seconds": 21600,
        "database": {
            "total_requests": 42,
            "total_responses": 40,
            "successful": 38,
            "failed": 2,
        },
    })

    result = runner.invoke(app, ["health"])
    assert result.exit_code == 0
    assert "healthy" in result.output
    assert "True" in result.output
    assert "42" in result.output


def test_health_unreachable(monkeypatch):
    _patch_client(monkeypatch, "health", side_effect=ConnectionError("refused"))

    result = runner.invoke(app, ["health"])
    assert result.exit_code == 1
    assert "Cannot reach" in result.output


# ---------------------------------------------------------------------------
# workflow
# ---------------------------------------------------------------------------


def test_workflow_dry_run():
    result = runner.invoke(app, [
        "query", "workflow", "-P", "Trieste", "-C", "TRIESTE", "-F", "9", "-p", "166",
        "-t", "F", "--dry-run",
    ])
    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert "Phase 1" in result.output
    assert "Phase 2" in result.output


def test_workflow_fabbricati_full(monkeypatch):
    """Workflow: search finds 2 sub-units, intestati fetched for each."""
    from sister.client import VisuraClient

    call_log = []

    async def fake_search(self, **kwargs):
        call_log.append(("search", kwargs))
        return {"request_ids": ["req_F_001"], "status": "queued"}

    async def fake_wait(self, request_id, **kwargs):
        call_log.append(("wait", request_id))
        if request_id == "req_F_001":
            return {
                "request_id": "req_F_001",
                "tipo_catasto": "F",
                "status": "completed",
                "data": {
                    "immobili": [
                        {"Foglio": "9", "Particella": "166", "Sub": "3"},
                        {"Foglio": "9", "Particella": "166", "Sub": "5"},
                    ],
                    "intestati": [],
                },
            }
        # intestati results
        return {
            "request_id": request_id,
            "status": "completed",
            "data": {"intestati": [{"Nominativo": "ROSSI MARIO"}]},
        }

    async def fake_intestati(self, **kwargs):
        call_log.append(("intestati", kwargs))
        rid = f"int_{kwargs.get('subalterno', 'x')}"
        return {"request_id": rid, "status": "queued"}

    monkeypatch.setattr(VisuraClient, "search", fake_search)
    monkeypatch.setattr(VisuraClient, "wait_for_result", fake_wait)
    monkeypatch.setattr(VisuraClient, "intestati", fake_intestati)

    result = runner.invoke(app, [
        "query", "workflow", "-P", "Trieste", "-C", "TRIESTE", "-F", "9", "-p", "166", "-t", "F",
    ])
    assert result.exit_code == 0
    assert "Phase 1" in result.output
    assert "Phase 2" in result.output
    assert "Workflow complete" in result.output

    # Should have submitted intestati for sub 3 and sub 5
    intestati_calls = [c for c in call_log if c[0] == "intestati"]
    subs = sorted(c[1]["subalterno"] for c in intestati_calls)
    assert subs == ["3", "5"]


def test_workflow_specific_subalterno(monkeypatch):
    """Workflow with --subalterno only fetches intestati for that sub."""
    from sister.client import VisuraClient

    async def fake_search(self, **kwargs):
        return {"request_ids": ["req_F_001"], "status": "queued"}

    async def fake_wait(self, request_id, **kwargs):
        if request_id == "req_F_001":
            return {
                "request_id": "req_F_001",
                "tipo_catasto": "F",
                "status": "completed",
                "data": {
                    "immobili": [
                        {"Foglio": "9", "Particella": "166", "Sub": "3"},
                        {"Foglio": "9", "Particella": "166", "Sub": "5"},
                    ],
                    "intestati": [],
                },
            }
        return {"request_id": request_id, "status": "completed", "data": {"intestati": []}}

    intestati_calls = []

    async def fake_intestati(self, **kwargs):
        intestati_calls.append(kwargs)
        return {"request_id": "int_3", "status": "queued"}

    monkeypatch.setattr(VisuraClient, "search", fake_search)
    monkeypatch.setattr(VisuraClient, "wait_for_result", fake_wait)
    monkeypatch.setattr(VisuraClient, "intestati", fake_intestati)

    result = runner.invoke(app, [
        "query", "workflow", "-P", "Trieste", "-C", "TRIESTE", "-F", "9", "-p", "166",
        "-t", "F", "-sub", "3",
    ])
    assert result.exit_code == 0
    assert len(intestati_calls) == 1
    assert intestati_calls[0]["subalterno"] == "3"


def test_workflow_no_immobili_skips_phase2(monkeypatch):
    """Workflow with empty search results skips Phase 2."""
    from sister.client import VisuraClient

    async def fake_search(self, **kwargs):
        return {"request_ids": ["req_T_001"], "status": "queued"}

    async def fake_wait(self, request_id, **kwargs):
        return {
            "request_id": request_id,
            "tipo_catasto": "T",
            "status": "completed",
            "data": {"immobili": [], "error": "NESSUNA CORRISPONDENZA TROVATA"},
        }

    monkeypatch.setattr(VisuraClient, "search", fake_search)
    monkeypatch.setattr(VisuraClient, "wait_for_result", fake_wait)

    result = runner.invoke(app, [
        "query", "workflow", "-P", "Roma", "-C", "ROMA", "-F", "999", "-p", "999", "-t", "T",
    ])
    assert result.exit_code == 0
    assert "No immobili found" in result.output


def test_workflow_writes_output(monkeypatch, tmp_path):
    """Workflow saves aggregated results to file."""
    from sister.client import VisuraClient

    async def fake_search(self, **kwargs):
        return {"request_ids": ["req_T_001"], "status": "queued"}

    async def fake_wait(self, request_id, **kwargs):
        if request_id == "req_T_001":
            return {
                "request_id": "req_T_001",
                "tipo_catasto": "T",
                "status": "completed",
                "data": {"immobili": [{"Foglio": "1", "Particella": "1"}], "intestati": []},
            }
        return {"request_id": request_id, "status": "completed", "data": {"intestati": [{"Nome": "TEST"}]}}

    async def fake_intestati(self, **kwargs):
        return {"request_id": "int_T", "status": "queued"}

    monkeypatch.setattr(VisuraClient, "search", fake_search)
    monkeypatch.setattr(VisuraClient, "wait_for_result", fake_wait)
    monkeypatch.setattr(VisuraClient, "intestati", fake_intestati)

    out_file = tmp_path / "workflow.json"
    result = runner.invoke(app, [
        "query", "workflow", "-P", "Roma", "-C", "ROMA", "-F", "1", "-p", "1", "-t", "T",
        "-o", str(out_file),
    ])
    assert result.exit_code == 0

    written = json.loads(out_file.read_text())
    assert "immobili" in written
    assert "intestati_results" in written


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------


def test_requests_lists_all(monkeypatch):
    _patch_client(monkeypatch, "history", {
        "results": [
            {
                "request_id": "req_F_001",
                "request_type": "visura",
                "tipo_catasto": "F",
                "provincia": "Trieste",
                "comune": "TRIESTE",
                "foglio": "9",
                "particella": "166",
                "subalterno": None,
                "success": True,
                "responded_at": "2025-01-01T12:00:00",
                "requested_at": "2025-01-01T11:59:00",
            },
            {
                "request_id": "req_T_002",
                "request_type": "visura",
                "tipo_catasto": "T",
                "provincia": "Roma",
                "comune": "ROMA",
                "foglio": "50",
                "particella": "10",
                "subalterno": None,
                "success": None,
                "responded_at": None,
                "requested_at": "2025-01-01T12:01:00",
            },
        ],
        "count": 2,
    })

    result = runner.invoke(app, ["requests"])
    assert result.exit_code == 0
    assert "req_F_001" in result.output
    assert "req_T_002" in result.output
    assert "completed" in result.output
    assert "pending" in result.output


def test_requests_filter_by_status_completed(monkeypatch):
    _patch_client(monkeypatch, "history", {
        "results": [
            {"request_id": "r1", "request_type": "visura", "tipo_catasto": "F",
             "provincia": "X", "comune": "X", "foglio": "1", "particella": "1",
             "subalterno": None, "success": True, "responded_at": "2025-01-01",
             "requested_at": "2025-01-01"},
            {"request_id": "r2", "request_type": "visura", "tipo_catasto": "T",
             "provincia": "X", "comune": "X", "foglio": "1", "particella": "1",
             "subalterno": None, "success": None, "responded_at": None,
             "requested_at": "2025-01-01"},
        ],
        "count": 2,
    })

    result = runner.invoke(app, ["requests", "--status", "completed"])
    assert result.exit_code == 0
    assert "r1" in result.output
    assert "r2" not in result.output


def test_requests_filter_by_status_pending(monkeypatch):
    _patch_client(monkeypatch, "history", {
        "results": [
            {"request_id": "r1", "request_type": "visura", "tipo_catasto": "F",
             "provincia": "X", "comune": "X", "foglio": "1", "particella": "1",
             "subalterno": None, "success": True, "responded_at": "2025-01-01",
             "requested_at": "2025-01-01"},
            {"request_id": "r2", "request_type": "visura", "tipo_catasto": "T",
             "provincia": "X", "comune": "X", "foglio": "1", "particella": "1",
             "subalterno": None, "success": None, "responded_at": None,
             "requested_at": "2025-01-01"},
        ],
        "count": 2,
    })

    result = runner.invoke(app, ["requests", "--status", "pending"])
    assert result.exit_code == 0
    assert "r2" in result.output
    assert "r1" not in result.output


def test_requests_empty(monkeypatch):
    _patch_client(monkeypatch, "history", {"results": [], "count": 0})

    result = runner.invoke(app, ["requests"])
    assert result.exit_code == 0
    assert "No requests found" in result.output


def test_requests_with_output(monkeypatch, tmp_path):
    _patch_client(monkeypatch, "history", {
        "results": [
            {"request_id": "r1", "request_type": "visura", "tipo_catasto": "T",
             "provincia": "X", "comune": "X", "foglio": "1", "particella": "1",
             "subalterno": None, "success": True, "responded_at": "2025-01-01",
             "requested_at": "2025-01-01"},
        ],
        "count": 1,
    })

    out_file = tmp_path / "requests.json"
    result = runner.invoke(app, ["requests", "-o", str(out_file)])
    assert result.exit_code == 0

    written = json.loads(out_file.read_text())
    assert written["count"] == 1


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------


def test_batch_dry_run(tmp_path):
    csv_file = tmp_path / "input.csv"
    csv_file.write_text(
        "provincia,comune,foglio,particella,tipo_catasto\n"
        "Trieste,TRIESTE,9,166,F\n"
        "Roma,ROMA,100,50,T\n"
    )

    result = runner.invoke(app, ["query", "batch", "-I", str(csv_file), "--dry-run"])
    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert "2" in result.output


def test_batch_submits_rows(monkeypatch, tmp_path):
    from sister.client import VisuraClient

    submitted = []

    async def fake_search(self, **kwargs):
        submitted.append(kwargs)
        return {"request_ids": [f"req_{len(submitted)}"], "status": "queued"}

    monkeypatch.setattr(VisuraClient, "search", fake_search)

    csv_file = tmp_path / "input.csv"
    csv_file.write_text(
        "provincia,comune,foglio,particella\n"
        "Trieste,TRIESTE,9,166\n"
        "Roma,ROMA,100,50\n"
    )

    result = runner.invoke(app, ["query", "batch", "-I", str(csv_file)])
    assert result.exit_code == 0
    assert len(submitted) == 2
    assert "Batch complete" in result.output


def test_batch_with_wait(monkeypatch, tmp_path):
    from sister.client import VisuraClient

    async def fake_search(self, **kwargs):
        return {"request_ids": ["req_1"], "status": "queued"}

    async def fake_wait(self, request_id, **kwargs):
        return {"request_id": request_id, "status": "completed", "data": {"immobili": []}}

    monkeypatch.setattr(VisuraClient, "search", fake_search)
    monkeypatch.setattr(VisuraClient, "wait_for_result", fake_wait)

    csv_file = tmp_path / "input.csv"
    csv_file.write_text("provincia,comune,foglio,particella\nTrieste,TRIESTE,9,166\n")

    result = runner.invoke(app, ["query", "batch", "-I", str(csv_file), "--wait"])
    assert result.exit_code == 0
    assert "Completed" in result.output


def test_batch_file_not_found():
    result = runner.invoke(app, ["query", "batch", "-I", "/nonexistent/file.csv"])
    assert result.exit_code == 1
    assert "File not found" in result.output


def test_batch_skips_comment_lines(monkeypatch, tmp_path):
    from sister.client import VisuraClient

    submitted = []

    async def fake_search(self, **kwargs):
        submitted.append(kwargs)
        return {"request_ids": [f"req_{len(submitted)}"], "status": "queued"}

    monkeypatch.setattr(VisuraClient, "search", fake_search)

    csv_file = tmp_path / "input.csv"
    csv_file.write_text(
        "# This is a comment\n"
        "provincia,comune,foglio,particella\n"
        "# Another comment\n"
        "Trieste,TRIESTE,9,166\n"
    )

    result = runner.invoke(app, ["query", "batch", "-I", str(csv_file)])
    assert result.exit_code == 0
    assert len(submitted) == 1


def test_batch_output_dir(monkeypatch, tmp_path):
    from sister.client import VisuraClient

    async def fake_search(self, **kwargs):
        return {"request_ids": ["req_1"], "status": "queued"}

    async def fake_wait(self, request_id, **kwargs):
        return {"request_id": request_id, "status": "completed", "data": {"ok": True}}

    monkeypatch.setattr(VisuraClient, "search", fake_search)
    monkeypatch.setattr(VisuraClient, "wait_for_result", fake_wait)

    csv_file = tmp_path / "input.csv"
    csv_file.write_text("provincia,comune,foglio,particella\nTrieste,TRIESTE,9,166\n")

    out_dir = tmp_path / "results"
    result = runner.invoke(app, [
        "query", "batch", "-I", str(csv_file), "--wait", "-O", str(out_dir),
    ])
    assert result.exit_code == 0

    files = list(out_dir.glob("*.json"))
    assert len(files) == 1
