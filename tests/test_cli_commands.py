"""Command-level tests for CLI surfaces not covered by the main query flow suite."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import sister.cli as cli
from sister.cli import app
from sister.query_forms import QUERY_FORMS, option_flags

runner = CliRunner()


def _dry_run_arguments(form):
    """Build the smallest valid CLI invocation for a generated query form."""
    params = {}
    required_names = list(form.required)
    required_names.extend(group[0] for group in form.required_any)
    for name in required_names:
        field = next((field for field in form.fields if field.param == name), None)
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
        params[name] = value

    args = ["query", form.command]
    for name, value in params.items():
        args.extend((option_flags(name)[0], value))
    return [*args, "--dry-run"]


def _cli_tree(command, path=()):
    rows = []
    command_path = (*path, command.name or "sister")
    params = []
    for param in command.params:
        flags = [*getattr(param, "opts", ()), *getattr(param, "secondary_opts", ())]
        params.append(
            {
                "name": param.name,
                "flags": flags,
                "required": bool(getattr(param, "required", False)),
                "hidden": bool(getattr(param, "hidden", False)),
            }
        )
    rows.append({"path": " ".join(command_path), "params": params})
    for name, child in sorted(getattr(command, "commands", {}).items()):
        rows.extend(_cli_tree(child, command_path))
    return rows


@pytest.mark.parametrize("form", QUERY_FORMS.values(), ids=lambda form: form.command)
def test_generated_query_commands_accept_required_options_and_show_api_v1(form):
    result = runner.invoke(app, _dry_run_arguments(form))

    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert f"/api/v1{form.path}" in result.output


def test_queries_lists_versioned_json_endpoints_and_root_health():
    result = runner.invoke(app, ["queries"])

    assert result.exit_code == 0
    assert "/api/v1/visura" in result.output
    assert "/health" in result.output
    assert "/api/v1/health" not in result.output


def test_cli_tree_matches_help_snapshot():
    from typer.main import get_command

    fixture = Path(__file__).parent / "fixtures" / "cli_tree.json"
    expected = json.loads(fixture.read_text(encoding="utf-8"))

    assert _cli_tree(get_command(app)) == expected


def test_run_session_internal_cli_contract_is_pinned():
    from typer.main import get_command

    from sister.cli import EXIT_SESSION_EXPIRED
    from sister.command_tools.run_in_session import EXIT_SESSION_EXPIRED as session_expired_code

    root = get_command(app)
    query = root.commands["query"]
    batch_command = query.commands["batch"]
    batch_flags = {flag for param in batch_command.params for flag in [*param.opts, *param.secondary_opts]}
    assert {"--input", "-I", "--resume", "--output", "-o", "--stop-at"} <= batch_flags

    subject_command = query.commands["soggetto"]
    subject_flags = {flag for param in subject_command.params for flag in [*param.opts, *param.secondary_opts]}
    assert {"--cf", "--wait", "-w", "--yes", "--no", "-o"} <= subject_flags
    assert EXIT_SESSION_EXPIRED == session_expired_code == 75

    root_path = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "sister.cli", "--help"],
        cwd=root_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Usage: python -m sister.cli" in result.stdout


def test_cli_run_api_uses_one_managed_client_context():
    from sister.cli import _runtime

    class FakeClient:
        base_url = "http://sister.test"

        def __init__(self):
            self.entered = 0
            self.exited = 0

        async def __aenter__(self):
            self.entered += 1
            return self

        async def __aexit__(self, *_exc):
            self.exited += 1

        async def result(self):
            return {"ok": True}

    client = FakeClient()
    result = _runtime.run_api(lambda active: active.result(), client=client)

    assert result == {"ok": True}
    assert client.entered == client.exited == 1


def test_cli_run_api_maps_service_connect_error_to_exit_3():
    import httpx
    import typer

    from sister.cli import _runtime

    class FakeClient:
        base_url = "http://sister.test"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def fail(self):
            raise httpx.ConnectError("offline")

    with pytest.raises(typer.Exit) as raised:
        _runtime.run_api(lambda active: active.fail(), client=FakeClient())

    assert raised.value.exit_code == 3


def test_cli_run_db_maps_missing_dsn_to_exit_3(monkeypatch):
    import typer

    from sister import database
    from sister.cli import _runtime

    async def missing_dsn():
        raise RuntimeError("SISTER_DATABASE_DSN_LOCALHOST must be set to a PostgreSQL connection URL")

    monkeypatch.setattr(database, "init_db", missing_dsn)
    with pytest.raises(typer.Exit) as raised:
        _runtime.run_db(lambda: None)

    assert raised.value.exit_code == 3


def test_health_uses_exit_3_when_service_cannot_be_reached(monkeypatch):
    import httpx

    from sister.cli import VisuraClient

    async def offline(self):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(VisuraClient, "health", offline)
    result = runner.invoke(app, ["health"])

    assert result.exit_code == 3
    assert "Service not reachable" in result.output


@pytest.mark.parametrize(
    ("command_args", "search_type", "expected"),
    [
        (
            ["query", "ipotecaria-immobile", "-P", "Trieste", "-C", "TRIESTE", "-F", "9", "-p", "166", "-t", "F"],
            "immobile",
            {"provincia": "Trieste", "comune": "TRIESTE", "foglio": "9", "particella": "166", "tipo_catasto": "F"},
        ),
        (
            ["query", "ipotecaria-persona", "-P", "Trieste", "--cf", "RSSMRA85M01H501Z"],
            "persona_fisica",
            {"provincia": "Trieste", "codice_fiscale": "RSSMRA85M01H501Z"},
        ),
        (
            ["query", "ipotecaria-azienda", "-P", "Trieste", "--id", "01234567890"],
            "persona_giuridica",
            {"provincia": "Trieste", "identificativo": "01234567890"},
        ),
        (
            ["query", "ipotecaria-nota", "-P", "Trieste", "--numero", "123", "--anno", "2024"],
            "nota",
            {"provincia": "Trieste", "numero_nota": "123", "anno_nota": "2024"},
        ),
    ],
    ids=["immobile", "persona", "azienda", "nota"],
)
def test_paid_inspection_commands_warn_and_forward_parameters(monkeypatch, command_args, search_type, expected):
    from sister.client import VisuraClient

    calls = []

    async def fake_inspection(self, **kwargs):
        calls.append(kwargs)
        return {"request_id": "inspection-1", "status": "queued"}

    monkeypatch.setattr(VisuraClient, "ispezione_ipotecaria", fake_inspection)

    result = runner.invoke(app, command_args)

    assert result.exit_code == 0, result.output
    assert "Preview only" in result.output
    assert "signed-in admin" in result.output
    assert "flow" in result.output
    assert "does not authorize a payment" in result.output
    assert calls[0]["tipo_ricerca"] == search_type
    assert calls[0]["auto_confirm"] is False
    assert {key: calls[0][key] for key in expected} == expected


def test_paid_inspection_yes_never_confirms_and_wait_writes_preview(monkeypatch, tmp_path):
    from sister.client import VisuraClient

    calls = []

    async def fake_inspection(self, **kwargs):
        calls.append(kwargs)
        return {"request_id": "inspection-yes", "status": "queued"}

    async def fake_wait(self, request_id, **kwargs):
        calls.append({"waited_for": request_id})
        return {
            "request_id": request_id,
            "status": "completed",
            "data": {"cost": {"text": "Costo", "value": 1.25}, "confirmed": False},
        }

    monkeypatch.setattr(VisuraClient, "ispezione_ipotecaria", fake_inspection)
    monkeypatch.setattr(VisuraClient, "wait_for_result", fake_wait)
    output = tmp_path / "nested" / "inspection.json"

    result = runner.invoke(
        app,
        ["query", "ipotecaria-persona", "-P", "Trieste", "--cf", "RSSMRA85M01H501Z", "--yes", "--wait", "-o", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["auto_confirm"] is False
    assert calls[1] == {"waited_for": "inspection-yes"}
    assert "Cost: Costo (€1.25)" in result.output
    assert "Cost not confirmed" in result.output
    assert json.loads(output.read_text()) == {
        "request_id": "inspection-yes",
        "status": "completed",
        "data": {"cost": {"text": "Costo", "value": 1.25}, "confirmed": False},
    }


def test_paid_inspection_help_hides_legacy_yes_flag():
    result = runner.invoke(app, ["query", "ipotecaria-persona", "--help"])

    assert result.exit_code == 0, result.output
    assert "--yes" not in result.output
    assert "-y" not in result.output


def test_mortgage_save_quote_forwards_exact_portal_request_id(monkeypatch):
    from sister.client import VisuraClient

    calls = []

    async def fake_quote(self, flow_id, **kwargs):
        calls.append((flow_id, kwargs))
        return {"quote": {"portal_request_id": kwargs["portal_request_id"]}}

    monkeypatch.setattr(VisuraClient, "mortgage_search_quote", fake_quote)
    result = runner.invoke(
        app,
        ["mortgage", "quote", "18", "--step", "richieste_save", "--portal-request-id", "REQ-888"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            18,
            {
                "step": "richieste_save",
                "ordinals": None,
                "ordinal": None,
                "portal_request_id": "REQ-888",
                "format": "PDF",
            },
        )
    ]
    assert "REQ-888" in result.output


def test_workflow_preset_dry_run_builds_plan_without_sending(monkeypatch):
    from sister import workflows

    captured = {}

    def fake_prepare(params):
        captured.update(params)
        return SimpleNamespace(description="Parcel due diligence", steps=["search", "intestati", "history"])

    monkeypatch.setattr(workflows, "prepare_workflow", fake_prepare)
    result = runner.invoke(
        app,
        [
            "query",
            "workflow",
            "--preset",
            "due-diligence",
            "-P",
            "Trieste",
            "-C",
            "TRIESTE",
            "-F",
            "9",
            "-p",
            "166",
            "--history",
            "--include-paid",
            "--confirm-paid",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "POST /api/v1/catasto/workflow/run" in result.output
    assert "search → intestati → history" in result.output
    assert captured["preset"] == "due-diligence"
    assert captured["include_history"] is True
    assert captured["include_paid_steps"] is True
    assert captured["auto_confirm"] is True


@pytest.mark.parametrize("legacy_flag", ["--yes", "-y"])
def test_workflow_legacy_yes_flag_fails_with_migration_message(monkeypatch, legacy_flag):
    from sister.client import VisuraClient

    calls = []

    async def fake_workflow(self, **kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(VisuraClient, "workflow", fake_workflow)
    result = runner.invoke(app, ["query", "workflow", "--preset", "due-diligence", legacy_flag])

    assert result.exit_code == 2, result.output
    assert "Use --confirm-paid instead" in result.output
    assert calls == []


def test_workflow_paid_deep_run_requires_confirmation_before_request(monkeypatch):
    from sister.client import VisuraClient

    calls = []

    async def fake_workflow(self, **kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(VisuraClient, "workflow", fake_workflow)
    result = runner.invoke(
        app,
        ["query", "workflow", "--preset", "due-diligence", "--depth", "deep", "--include-paid"],
    )

    assert result.exit_code == 2, result.output
    assert "--depth deep --include-paid requires --confirm-paid" in result.output
    assert calls == []


def test_workflow_help_hides_legacy_paid_confirmation_flag():
    result = runner.invoke(app, ["query", "workflow", "--help"])

    assert result.exit_code == 0, result.output
    assert "--confirm-paid" in result.output
    assert "--yes" not in result.output


def test_workflow_preset_submission_formats_results_and_writes_output(monkeypatch, tmp_path):
    from sister.client import VisuraClient

    calls = []

    async def fake_workflow(self, **kwargs):
        calls.append(kwargs)
        return {
            "steps": [
                {
                    "step": "search",
                    "status": "completed",
                    "data": {"immobili": [{}, {}], "total": 2, "truncated": True},
                },
                {"step": "owners", "status": "error", "error": "owner lookup failed"},
                {"step": "history", "status": "skipped"},
                {"step": "drill", "status": "completed", "data": {"drill_results": [{}], "intestati": [{}]}},
            ],
            "summary": {"completed": 2, "failed": 1, "skipped": 1, "properties": 2, "owners": 1, "risk_flags": 3},
        }

    monkeypatch.setattr(VisuraClient, "workflow", fake_workflow)
    output = tmp_path / "workflow" / "result.json"
    result = runner.invoke(
        app,
        [
            "query",
            "workflow",
            "--preset",
            "due-diligence",
            "-P",
            "Trieste",
            "-C",
            "TRIESTE",
            "-F",
            "9",
            "-p",
            "166",
            "--history",
            "--include-paid",
            "--confirm-paid",
            "--depth",
            "deep",
            "-o",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["preset"] == "due-diligence"
    assert calls[0]["include_history"] is True
    assert calls[0]["include_paid_steps"] is True
    assert calls[0]["depth"] == "deep"
    assert calls[0]["auto_confirm"] is True
    assert "immobili: 2 records" in result.output
    assert "total: 2" in result.output
    assert "Results truncated" in result.output
    assert "owner lookup failed" in result.output
    assert "Risk flags: 3" in result.output
    assert json.loads(output.read_text())["summary"]["properties"] == 2


def test_workflow_preset_maps_api_errors_to_cli_failure(monkeypatch):
    from sister.client import VisuraAPIError, VisuraClient

    async def fake_workflow(self, **kwargs):
        raise VisuraAPIError(422, "invalid workflow request")

    monkeypatch.setattr(VisuraClient, "workflow", fake_workflow)
    result = runner.invoke(app, ["query", "workflow", "--preset", "due-diligence", "-P", "Trieste"])

    assert result.exit_code == 1
    assert "HTTP 422" in result.output


def test_workflow_preset_reports_invalid_plan(monkeypatch):
    from sister import workflows

    def fail_prepare(params):
        raise ValueError("missing parcel")

    monkeypatch.setattr(workflows, "prepare_workflow", fail_prepare)
    result = runner.invoke(app, ["query", "workflow", "--preset", "due-diligence", "--dry-run"])

    assert result.exit_code == 1
    assert "Invalid workflow: missing parcel" in result.output


@pytest.mark.parametrize(
    ("group", "name", "target", "shell"),
    [
        (group.info.name, name, command[0], bool(command[2]) if len(command) > 2 else False)
        for group, commands in cli._tool_commands.items()
        for name, command in commands.items()
    ],
    ids=[f"{group.info.name}-{name}" for group, commands in cli._tool_commands.items() for name in commands],
)
def test_registered_tool_commands_forward_to_their_target(monkeypatch, tmp_path, group, name, target, shell):
    from sister.cli import _runtime

    monkeypatch.setattr(_runtime, "project_root", lambda: tmp_path)
    calls = []

    def fake_run(command, *, cwd, check):
        calls.append((command, cwd, check))
        return SimpleNamespace(returncode=7)

    from sister.cli import tools

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    result = runner.invoke(app, [group, name, "--example-option", "value"])

    expected = ["bash", str(tmp_path / target)] if shell else [sys.executable, "-m", target]
    expected.extend(("--example-option", "value"))
    assert result.exit_code == 7
    assert calls == [(expected, tmp_path, False)]


def test_forwarded_command_converts_keyboard_interrupt_to_exit_130(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_project_root", lambda: tmp_path)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.subprocess, "run", interrupt)
    result = runner.invoke(app, ["app", "login"])

    assert result.exit_code == 130


@pytest.mark.parametrize("command_name", ["init", "migrate"])
def test_db_upgrade_commands_apply_head(monkeypatch, command_name):
    from alembic import command as alembic_command

    calls = []
    monkeypatch.setattr(alembic_command, "upgrade", lambda config, revision: calls.append((config, revision)))

    result = runner.invoke(app, ["db", command_name])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][1] == "head"
    assert calls[0][0].config_file_name.endswith("alembic.ini")
    assert "applied" in result.output.lower()


def test_db_status_reports_current_migration(monkeypatch):
    from alembic import command as alembic_command

    calls = []
    monkeypatch.setattr(alembic_command, "current", lambda config, *, verbose: calls.append((config, verbose)))

    result = runner.invoke(app, ["db", "status"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0].config_file_name.endswith("alembic.ini")
    assert calls[0][1] is True


def test_db_backfill_runs_both_projections_and_xml_and_records_activity(monkeypatch):
    from sister import database, xml_ingest

    calls = {}

    async def fake_projections(*, limit, contains):
        calls["projections"] = (limit, contains)
        return 3

    async def fake_mortgage_searches(*, limit):
        calls["mortgage_searches"] = limit
        return {"responses": 2, "searches": 2, "formalities": 4, "annotations": 1, "document_requests": 0}

    async def fake_xml(*, force, limit):
        calls["xml"] = (force, limit)
        return 5

    async def fake_activity(*args, **kwargs):
        calls["activity"] = (args, kwargs)

    async def fake_init_db():
        calls["init_db"] = True

    async def fake_names():
        return {"nameless": 0}

    async def fake_holdings():
        return 7

    async def fake_history():
        return {"answers": 0}

    monkeypatch.setattr(database, "backfill_ownership_history", fake_history)  # the real one wipes and rebuilds the live history
    monkeypatch.setattr(database, "backfill_projections", fake_projections)
    monkeypatch.setattr(database, "backfill_mortgage_searches", fake_mortgage_searches)
    monkeypatch.setattr(xml_ingest, "backfill_documents", fake_xml)
    monkeypatch.setattr(database, "backfill_subject_names", fake_names)
    monkeypatch.setattr(database, "refresh_subject_holdings", fake_holdings)
    monkeypatch.setattr(database, "record_activity", fake_activity)
    monkeypatch.setattr(database, "init_db", fake_init_db)

    async def fake_mortgage_notes(*, force, limit):
        calls["mortgage_notes"] = (force, limit)
        return {"candidates": 1, "projected": 1}

    from sister import mortgage_note_projection

    monkeypatch.setattr(mortgage_note_projection, "backfill_mortgage_notes", fake_mortgage_notes)

    result = runner.invoke(app, ["db", "backfill", "--force", "--limit", "12"])

    assert result.exit_code == 0, result.output
    assert calls["projections"] == (12, None)
    assert calls["mortgage_searches"] == 12
    assert calls["xml"] == (True, 12)
    assert calls["activity"][0] == ("db.backfill",)
    assert calls["activity"][1]["params"] == {"what": "all", "force": True, "limit": 12}
    assert calls["activity"][1]["result"] == {
        "projections": 3,
        "mortgage_searches": {"responses": 2, "searches": 2, "formalities": 4, "annotations": 1, "document_requests": 0},
        "mortgage_notes": {"candidates": 1, "projected": 1},
        "xml": 5,
        "names": {"nameless": 0},
        "history": {"answers": 0},
        "holdings": 7,
    }


def test_db_backfill_mortgage_searches_is_scoped_and_limited(monkeypatch):
    from sister import database

    calls = []

    async def fake_mortgage_searches(*, limit):
        calls.append(limit)
        return {"responses": 1}

    async def fake_activity(*args, **kwargs):
        calls.append(kwargs)

    async def fake_init_db():
        return None

    monkeypatch.setattr(database, "backfill_mortgage_searches", fake_mortgage_searches)
    monkeypatch.setattr(database, "record_activity", fake_activity)
    monkeypatch.setattr(database, "init_db", fake_init_db)

    result = runner.invoke(app, ["db", "backfill", "mortgage-searches", "--limit", "2"])

    assert result.exit_code == 0, result.output
    assert calls[0] == 2
    assert calls[1]["params"] == {"what": "mortgage-searches", "force": False, "limit": 2}
    assert calls[1]["result"] == {"mortgage_searches": {"responses": 1}}


def test_db_backfill_rejects_unknown_target():
    result = runner.invoke(app, ["db", "backfill", "unknown"])

    assert result.exit_code == 2
    assert "usa projections" in result.output
    assert "mortgage-searches" in result.output
    assert "all" in result.output


def test_floor_plan_validate_passes_inputs_and_writes_report(monkeypatch, tmp_path):
    from sister import floor_plan

    captured = {}
    report = {
        "comparison": {
            "status": "match",
            "visura_area_m2": 50.0,
            "estimated_area_m2": 49.5,
            "difference_m2": 0.5,
        }
    }

    async def fake_validate(**kwargs):
        captured.update(kwargs)
        return report

    monkeypatch.setattr(floor_plan, "validate_visura_against_floor_plan", fake_validate)
    visura = tmp_path / "visura.json"
    visura.write_text(json.dumps({"request_id": "r1"}))
    calibration = tmp_path / "calibration.json"
    calibration.write_text(json.dumps({"scale": 2}))
    floor_plan_file = tmp_path / "plan.pdf"
    output = tmp_path / "report.json"

    result = runner.invoke(
        app,
        [
            "floor-plan-validate",
            "--visura",
            str(visura),
            "--floor-plan",
            str(floor_plan_file),
            "--calibration",
            str(calibration),
            "--property-index",
            "2",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["visura"] == {"request_id": "r1"}
    assert captured["floor_plan_path"] == floor_plan_file
    assert captured["calibration"] == {"scale": 2}
    assert captured["property_index"] == 2
    assert "Validation: match" in result.output
    assert json.loads(output.read_text()) == report


def test_floor_plan_validate_reports_invalid_json(tmp_path):
    visura = tmp_path / "visura.json"
    visura.write_text("not-json")

    result = runner.invoke(
        app,
        ["floor-plan-validate", "--visura", str(visura), "--floor-plan", str(tmp_path / "plan.pdf")],
    )

    assert result.exit_code == 1
    assert "Invalid floor-plan validation input" in result.output
