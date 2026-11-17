"""Root JSON mode keeps command data on stdout and progress on stderr."""

import json

import pytest
from typer.testing import CliRunner

from sister.cli import app
from sister.client import VisuraClient
from sister.query_forms import QUERY_FORMS, option_flags

runner = CliRunner()


def _stdout_json(result):
    assert result.stdout.strip(), result.stderr
    return json.loads(result.stdout)


def _generated_query_dry_run_args(form):
    names = list(form.required) + [group[0] for group in form.required_any]
    args = ["--json", "query", form.command]
    for name in names:
        field = next((candidate for candidate in form.fields if candidate.param == name), None)
        if field and field.choices:
            value = next(iter(field.choices))
        elif name == "tipo_catasto":
            value = "T"
        elif name == "provincia":
            value = "Trieste"
        elif name == "comune":
            value = "TRIESTE"
        else:
            value = "test-value"
        args.extend((option_flags(name)[0], str(value)))
    return [*args, "--dry-run"]


def test_health_json_is_the_only_stdout(monkeypatch):
    async def fake_health(self):
        return {"status": "healthy", "authenticated": True}

    monkeypatch.setattr(VisuraClient, "health", fake_health)
    result = runner.invoke(app, ["--json", "health"])

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result) == {"status": "healthy", "authenticated": True}
    assert "Stato API visure" in result.stderr


def test_generated_query_json_and_output_file(monkeypatch, tmp_path):
    async def fake_submit(self, command, params, **kwargs):
        return {"request_id": "request-1", "status": "queued", "command": command, "params": params}

    monkeypatch.setattr(VisuraClient, "submit", fake_submit)
    output = tmp_path / "result.json"
    result = runner.invoke(
        app,
        ["--json", "query", "search", "-P", "Trieste", "-C", "TRIESTE", "-F", "9", "-p", "166", "-o", str(output)],
    )

    assert result.exit_code == 0, result.stderr
    data = _stdout_json(result)
    assert data["request_id"] == "request-1"
    assert json.loads(output.read_text(encoding="utf-8")) == data
    assert "Richiesta inviata" in result.stderr
    assert "File di output scritto in" in result.stderr


@pytest.mark.parametrize("form", QUERY_FORMS.values(), ids=lambda form: form.command)
def test_every_generated_query_dry_run_has_one_json_document(form):
    result = runner.invoke(app, _generated_query_dry_run_args(form))

    assert result.exit_code == 0, result.stderr
    data = _stdout_json(result)
    assert data["dry_run"] is True
    assert data["command"] == form.command
    assert data["endpoint"].endswith(form.path)
    assert data["paid"] is form.paid


def test_requests_json_emits_the_filtered_rows(monkeypatch):
    async def fake_history(self, **kwargs):
        return {"count": 1, "results": [{"request_id": "request-1", "success": True}]}

    monkeypatch.setattr(VisuraClient, "history", fake_history)
    result = runner.invoke(app, ["--json", "requests", "--status", "completed"])

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result) == {"count": 1, "results": [{"request_id": "request-1", "success": True}]}


@pytest.mark.parametrize(
    ("args", "method", "kwargs", "expected"),
    [
        (["requests", "get", "request-1"], "get_result", {"request_id": "request-1"}, {"request_id": "request-1", "status": "completed"}),
        (["requests", "wait", "request-1"], "wait_for_result", {"request_id": "request-1"}, {"request_id": "request-1", "status": "completed"}),
    ],
)
def test_request_lookup_commands_emit_one_json_document(monkeypatch, args, method, kwargs, expected):
    async def fake(self, request_id, **_kwargs):
        assert request_id == kwargs["request_id"]
        return expected

    monkeypatch.setattr(VisuraClient, method, fake)
    result = runner.invoke(app, ["--json", *args])

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result) == expected


def test_queries_json_lists_endpoints():
    result = runner.invoke(app, ["--json", "queries"])

    assert result.exit_code == 0, result.stderr
    data = _stdout_json(result)
    assert data["service_url"]
    assert any(row["command"] == "query search" for row in data["commands"])
    assert next(row for row in data["commands"] if row["command"] == "query ipotecaria-immobile")["paid"] is True
    names = {row["command"] for row in data["commands"]}
    assert {"workflow run", "workflow status", "workflow cancel", "health"} <= names
    assert "query workflow" not in names
    assert "query ipotecaria-persona" not in names


def test_json_mode_can_be_enabled_by_environment(monkeypatch):
    async def fake_health(self):
        return {"status": "healthy", "authenticated": True}

    monkeypatch.setattr(VisuraClient, "health", fake_health)
    monkeypatch.setenv("SISTER_CLI_JSON", "1")
    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result)["authenticated"] is True


def test_preset_workflow_json(monkeypatch):
    async def fake_workflow(self, **_kwargs):
        return {"workflow_id": "workflow-1", "status": "completed", "steps": []}

    monkeypatch.setattr(VisuraClient, "workflow", fake_workflow)
    result = runner.invoke(
        app,
        ["--json", "workflow", "run", "--preset", "due-diligence", "-P", "Trieste", "-C", "TRIESTE", "-F", "9", "-p", "166"],
    )

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result)["workflow_id"] == "workflow-1"
    assert "ID workflow: workflow-1" in result.stderr


def test_preset_workflow_dry_run_is_json(monkeypatch):
    result = runner.invoke(
        app,
        ["--json", "workflow", "run", "--preset", "due-diligence", "-P", "Trieste", "-C", "TRIESTE", "-F", "9", "-p", "166", "--dry-run"],
    )

    assert result.exit_code == 0, result.stderr
    data = _stdout_json(result)
    assert data["dry_run"] is True and data["preset"] == "due-diligence"
    assert data["steps"]


@pytest.mark.parametrize(
    ("args", "method", "expected"),
    [
        (["workflow", "status", "workflow-1"], "workflow_status", {"workflow_id": "workflow-1", "status": "completed"}),
        (["workflow", "cancel", "workflow-1"], "cancel_workflow", {"workflow_id": "workflow-1", "status": "cancelled"}),
    ],
)
def test_workflow_management_commands_emit_one_json_document(monkeypatch, args, method, expected):
    async def fake(self, workflow_id):
        assert workflow_id == "workflow-1"
        return expected

    monkeypatch.setattr(VisuraClient, method, fake)
    result = runner.invoke(app, ["--json", *args])

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result) == expected


@pytest.mark.parametrize(
    ("args", "method", "expected"),
    [
        (["mortgage", "status", "42"], "mortgage_search_status", {"flow_id": 42, "status": "completed"}),
        (["mortgage", "quote", "42"], "mortgage_search_quote", {"flow_id": 42, "quote": {"total": 0}}),
    ],
)
def test_mortgage_data_commands_emit_one_json_document(monkeypatch, args, method, expected):
    async def fake(self, flow_id, **_kwargs):
        assert flow_id == 42
        return expected

    monkeypatch.setattr(VisuraClient, method, fake)
    result = runner.invoke(app, ["--json", *args])

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result) == expected


def test_db_status_json_emits_migration_revision(monkeypatch):
    from sister.cli import db

    payload = {
        "current": ["rev_1"],
        "heads": ["rev_1"],
        "database_revision": "rev_1",
        "revision_matches": True,
        "behind": False,
    }
    monkeypatch.setattr(db._runtime, "run_db", lambda *_args, **_kwargs: payload)
    result = runner.invoke(app, ["--json", "db", "status"])

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result) == payload


def test_db_backfill_json_emits_summary(monkeypatch):
    from sister.cli import db

    payload = {"projections": 3, "xml": 2}
    monkeypatch.setattr(db, "run_db", lambda _factory: payload)
    result = runner.invoke(app, ["--json", "db", "backfill", "projections"])

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result) == payload


def test_db_migrate_json_emits_status(monkeypatch):
    from alembic import command
    from sister.cli import db

    monkeypatch.setattr(db, "_alembic_config", lambda: object())
    monkeypatch.setattr(command, "upgrade", lambda *_args: None)
    result = runner.invoke(app, ["--json", "db", "migrate"])

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result) == {"status": "migrated"}


@pytest.mark.parametrize(
    ("args", "operation", "operation_args", "expected"),
    [
        (["ade", "statement", "import"], "import_statement_file", ("path",), {"import_id": 1, "rows_added": 2}),
        (["ade", "statement", "detail-import"], "import_statement_detail_file", ("statement_key", "path"), {"statement_key": "s1", "details_added": 2}),
        (["ade", "receipt", "import"], "import_receipt_file", ("path",), {"receipt_id": 7, "status": "imported"}),
    ],
)
def test_ade_import_commands_emit_one_json_document(monkeypatch, tmp_path, args, operation, operation_args, expected):
    import asyncio

    from sister import ade_ledger
    from sister.cli import ade

    source = tmp_path / "source.txt"
    source.write_text("fixture", encoding="utf-8")

    async def fake_statement(path):
        assert path == source
        return expected

    async def fake_detail(statement_key, path):
        assert (statement_key, path) == ("s1", source)
        return expected

    async def fake_receipt(path):
        assert path == source
        return expected

    monkeypatch.setattr(ade_ledger, operation, {
        "import_statement_file": fake_statement,
        "import_statement_detail_file": fake_detail,
        "import_receipt_file": fake_receipt,
    }[operation])
    monkeypatch.setattr(ade._runtime, "run_db", lambda factory: asyncio.run(factory()))
    command_args = ["--json", *args]
    if operation == "import_statement_detail_file":
        command_args.extend(("s1", str(source)))
    else:
        command_args.append(str(source))
    result = runner.invoke(app, command_args)

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result) == expected


def test_documents_import_receipts_json_emits_each_result(monkeypatch, tmp_path):
    import asyncio

    from sister import ade_ledger
    from sister.cli import documents

    receipt = tmp_path / "receipt.txt"
    receipt.write_text("fixture", encoding="utf-8")
    expected = [{"file": "receipt.txt", "receipt_id": 9, "status": "imported"}]

    async def fake_import(path):
        return {"receipt_id": 9, "status": "imported"}

    monkeypatch.setattr(ade_ledger, "import_receipt_file", fake_import)
    monkeypatch.setattr(documents._runtime, "run_db", lambda factory: asyncio.run(factory()))
    result = runner.invoke(app, ["--json", "documents", "import-receipts", str(receipt)])

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result) == expected


def test_documents_import_receipts_json_reports_an_empty_directory(tmp_path):
    result = runner.invoke(app, ["--json", "documents", "import-receipts", str(tmp_path)])

    assert result.exit_code == 1
    assert _stdout_json(result) == []
    assert "Nessuna ricevuta PDF/TXT trovata" in result.stderr


def test_ade_pagopa_link_json_emits_link_identity(monkeypatch):
    import asyncio

    from sister import ade_ledger
    from sister.cli import ade

    async def fake_link(statement_key, payment_id, *, linked_by):
        assert (statement_key, payment_id) == ("statement-1", 42)
        assert linked_by.startswith("cli:")

    monkeypatch.setattr(ade_ledger, "link_validated_pagopa", fake_link)
    monkeypatch.setattr(ade._runtime, "run_db", lambda factory: asyncio.run(factory()))
    result = runner.invoke(app, ["--json", "ade", "statement", "link-pagopa", "statement-1", "42"])

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result) == {"status": "linked", "statement_key": "statement-1", "payment_id": 42}


def test_floor_plan_validation_json_emits_comparison(monkeypatch, tmp_path):
    from sister.cli import documents

    visura = tmp_path / "visura.json"
    visura.write_text("{}", encoding="utf-8")
    floor_plan = tmp_path / "plan.pdf"
    expected = {"comparison": {"status": "match", "visura_area_m2": 60}}

    def fake_run(awaitable):
        awaitable.close()
        return expected

    monkeypatch.setattr(documents.asyncio, "run", fake_run)
    result = runner.invoke(
        app,
        ["--json", "documents", "floor-plan-validate", "--visura", str(visura), "--floor-plan", str(floor_plan)],
    )

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result) == expected


def test_batch_schema_json_and_output_file(monkeypatch, tmp_path):
    from sister import batch

    expected = {"type": "object", "properties": {"queries": {"type": "array"}}}
    monkeypatch.setattr(batch, "batch_json_schema", lambda: expected)
    output = tmp_path / "batch-schema.json"
    result = runner.invoke(app, ["--json", "query", "batch-schema", "--output", str(output)])

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result) == expected
    assert json.loads(output.read_text(encoding="utf-8")) == expected


def test_batch_json_emits_report_and_json_line_events(monkeypatch, tmp_path):
    async def fake_submit(self, command, params, **kwargs):
        return {"request_id": "request-1"}

    async def fake_wait(self, request_id, **kwargs):
        return {"request_id": request_id, "status": "completed"}

    monkeypatch.setattr(VisuraClient, "submit", fake_submit)
    monkeypatch.setattr(VisuraClient, "wait_for_result", fake_wait)
    source = tmp_path / "batch.json"
    source.write_text(
        json.dumps(
            {
                "options": {"wait": True},
                "queries": [{"params": {"provincia": "Trieste", "comune": "TRIESTE", "foglio": 9, "particella": 166}}],
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["--json", "query", "batch", "-I", str(source)])

    assert result.exit_code == 0, result.stderr
    report = _stdout_json(result)
    assert report["counts"] == {"completed": 1}
    event_lines = [line for line in result.stderr.splitlines() if line.startswith("{")]
    assert event_lines
    assert all(json.loads(line)["event"] in {"start", "submitted", "result", "done"} for line in event_lines)


def test_download_missing_p7m_json_uses_shared_output_emitter(monkeypatch, tmp_path):
    from sister.command_tools import download_missing_p7m

    report = {"results": [{"request_id": "request-1", "status": "completed"}], "completed": 1, "errors": 0}
    monkeypatch.setattr(download_missing_p7m, "run", lambda **_kwargs: (0, report))
    output = tmp_path / "p7m-results.json"
    result = runner.invoke(
        app,
        ["--json", "documents", "download-missing-p7m", "--output", str(output)],
    )

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result) == report
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_define_document_workflow_json_emits_summary_and_keeps_manifest_option(monkeypatch, tmp_path):
    from sister.command_tools import define_document_workflow_batch

    manifest = tmp_path / "manifest.jsonl"
    report = {"manifest": str(manifest), "jobs": 0, "paid_steps_deferred": True}
    monkeypatch.setattr(define_document_workflow_batch, "run", lambda **_kwargs: (0, report))
    result = runner.invoke(
        app,
        ["--json", "documents", "define-workflow", "--documents", str(tmp_path), "--manifest", str(manifest)],
    )

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result) == report


def test_app_hash_password_json_emits_the_generated_entry(monkeypatch):
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "secret")
    result = runner.invoke(app, ["--json", "app", "hash-password", "reader"])

    assert result.exit_code == 0, result.stderr
    data = _stdout_json(result)
    assert data["username"] == "reader"
    assert data["entry"].startswith("reader    pbkdf2_sha256$")


def test_app_hash_password_json_reports_mismatch(monkeypatch):
    import getpass

    answers = iter(("first", "second"))
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: next(answers))
    result = runner.invoke(app, ["--json", "app", "hash-password", "reader"])

    assert result.exit_code == 1
    assert _stdout_json(result) == {"error": "Le password non coincidono", "exit_code": 1}
    assert "non coincidono" not in result.stderr


def test_local_session_tool_rich_output_is_redirected_to_stderr(monkeypatch, tmp_path):
    from sister.command_tools import run_in_session

    def fake_run(**_kwargs):
        run_in_session.console.print("session report")
        return 0

    monkeypatch.setattr(run_in_session, "run", fake_run)
    result = runner.invoke(app, ["--json", "batch", "run-session", "--batch", str(tmp_path)])

    assert result.exit_code == 0, result.stderr
    assert _stdout_json(result) == {
        "command": "batch run-session",
        "exit_code": 0,
        "output": "session report\n",
    }
    assert "session report" in result.stderr
