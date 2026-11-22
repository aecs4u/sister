"""Command-level tests for CLI surfaces not covered by the main query flow suite."""

import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from sister.cli import app
from sister.query_forms import QUERY_FORMS, option_flags

runner = CliRunner()


def _plain(output):
    """Remove Rich's ANSI color codes before checking CLI text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", output)


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
    assert "SIMULAZIONE" in result.output
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

    tree = _cli_tree(get_command(app))
    paths = {item["path"].removeprefix("sister ") for item in tree}
    assert "batch run-list" in paths
    assert "query run-list" not in paths
    assert tree == expected


def test_legacy_sub_flag_is_hidden_and_still_fills_subalterno():
    base = ["query", "intestati", "-P", "Trieste", "-C", "TRIESTE", "-F", "9", "-p", "166", "-t", "F", "--dry-run"]

    legacy = runner.invoke(app, [*base, "-sub", "3"])
    assert legacy.exit_code == 0, legacy.output
    assert '"subalterno": "3"' in legacy.output
    assert "`-sub` è deprecato; usa `--subalterno`" in legacy.stderr

    for flag in ("--subalterno", "--sub"):
        current = runner.invoke(app, [*base, flag, "3"])
        assert current.exit_code == 0, current.output
        assert '"subalterno": "3"' in current.output
        assert "deprecato" not in current.stderr

    # the explicit long option wins when both are given
    both = runner.invoke(app, [*base, "--subalterno", "4", "-sub", "3"])
    assert '"subalterno": "4"' in both.output

    from typer.main import get_command

    help_text = _plain(runner.invoke(app, ["query", "intestati", "--help"]).output)
    assert "--subalterno" in help_text
    query = get_command(app).commands["query"]
    intestati = query.commands["intestati"]
    legacy_option = next(param for param in intestati.params if param.name == "legacy_subalterno")
    assert legacy_option.hidden is True
    assert "--sub" in next(param for param in intestati.params if param.name == "subalterno").opts


def test_root_help_groups_commands_and_documents_exit_codes():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Service" in result.output
    assert "Database" in result.output
    assert "Local files" in result.output
    assert "Checkout tools" in result.output
    assert "75 sessione scaduta" in result.output


def test_run_session_internal_cli_contract_is_pinned():
    from typer.main import get_command

    from sister.cli._runtime import EXIT_SESSION_EXPIRED
    from sister.command_tools.run_in_session import EXIT_SESSION_EXPIRED as session_expired_code

    root = get_command(app)
    query = root.commands["query"]
    batch_command = query.commands["batch"]
    batch_flags = {flag for param in batch_command.params for flag in [*param.opts, *param.secondary_opts]}
    assert {"--input", "-I", "--resume", "--output-dir", "-O", "--output", "-o", "--stop-at"} <= batch_flags

    subject_command = query.commands["soggetto"]
    subject_flags = {flag for param in subject_command.params for flag in [*param.opts, *param.secondary_opts]}
    assert {"--cf", "--wait", "-w", "--yes", "--no", "-o"} <= subject_flags

    run_session_flags = {
        flag
        for param in root.commands["batch"].commands["run-session"].params
        for flag in [*param.opts, *param.secondary_opts]
    }
    assert {
        "--batch",
        "--minutes",
        "--logged-in-at",
        "--grace-seconds",
        "--port",
        "--preflight-cf",
        "--stop-stale-service",
        "--cdp",
        "--email-progress",
        "--email-to",
        "--dry-run",
        "--keep-answered",
        "--probe",
    } <= run_session_flags
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
    assert "Usage: python -m sister.cli" in _plain(result.stdout)


def test_run_session_batch_child_uses_output_dir_for_resume(monkeypatch, tmp_path):
    from datetime import datetime, timedelta

    from sister.command_tools import run_in_session

    captured = {}

    class FinishedProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FinishedProcess()

    monkeypatch.setattr(run_in_session.subprocess, "Popen", fake_popen)
    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()
    deadline = datetime.now() + timedelta(minutes=5)

    assert run_in_session.run_batch(batch_dir, 8027, deadline) == "done"
    command = captured["command"]
    assert "--output-dir" in command
    assert command[command.index("--output-dir") + 1] == str(batch_dir)
    assert command[command.index("-o") + 1] == str(batch_dir / "report.json")


def test_batch_run_session_uses_in_process_run_function(monkeypatch, tmp_path):
    from sister.command_tools import run_in_session

    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(run_in_session, "run", fake_run)
    first, second = tmp_path / "one", tmp_path / "two"
    result = runner.invoke(
        app,
        [
            "batch",
            "run-session",
            "--batch",
            str(first),
            "--batch",
            str(second),
            "--minutes",
            "24",
            "--logged-in-at",
            "17:43",
            "--stop-stale-service",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["batch"] == [first, second]
    assert captured["minutes"] == 24
    assert captured["logged_in_at"] == "17:43"
    assert captured["stop_stale_service"] is True
    assert captured["dry_run"] is True


def test_batch_run_session_help_is_typer_help():
    result = runner.invoke(app, ["batch", "run-session", "--help"])

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "Usage: sister batch run-session" in output
    assert "--stop-stale-service" in output


def test_batch_progress_uses_in_process_run_function(monkeypatch, tmp_path):
    from sister.command_tools import batch_progress

    captured = {}

    def fake_run(directories, **kwargs):
        captured["directories"] = directories
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(batch_progress, "run", fake_run)
    directories = [tmp_path / "one", tmp_path / "two"]
    result = runner.invoke(
        app, ["batch", "progress", *(str(path) for path in directories), "--once", "--interval", "1"]
    )

    assert result.exit_code == 0, result.output
    assert captured == {"directories": directories, "once": True, "interval": 1.0}


def test_batch_progress_help_is_typer_help():
    result = runner.invoke(app, ["batch", "progress", "--help"])

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "Usage: sister batch progress" in output
    assert "--once" in output


def test_batch_build_list_uses_in_process_run_function(monkeypatch, tmp_path):
    from sister.command_tools import build_batch_from_list

    calls = []
    monkeypatch.setattr(build_batch_from_list, "run", lambda *args, **kwargs: calls.append((args, kwargs)) or 0)
    list_file, output_dir = tmp_path / "list.md", tmp_path / "batch"
    result = runner.invoke(
        app,
        ["batch", "build-list", str(list_file), "--output-dir", str(output_dir), "--extra", "all", "--captcha-free"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            (list_file, output_dir),
            {"tipo_richiesta": None, "extra": "all", "captcha_free": True, "name": None},
        )
    ]


def test_batch_run_list_uses_build_and_session_functions_in_process(monkeypatch, tmp_path):
    from sister.command_tools import build_batch_from_list, run_in_session, run_list

    calls = {"build": [], "session": []}

    def fake_build(*args, **kwargs):
        calls["build"].append((args, kwargs))
        return 0

    def fake_session(**kwargs):
        calls["session"].append(kwargs)
        return 0

    monkeypatch.setattr(build_batch_from_list, "run", fake_build)
    monkeypatch.setattr(run_in_session, "run", fake_session)
    source, batch_dir = tmp_path / "list.md", tmp_path / "batch"
    source.write_text("# test\n", encoding="utf-8")
    result = run_list.run(
        list_file=source,
        batch_dir=batch_dir,
        tipo_richiesta="storica",
        extra="all",
        captcha_free=True,
        stop_stale_service=True,
        logged_in_at="14:05",
    )

    assert result == 0
    assert calls["build"] == [
        ((source.resolve(), batch_dir.resolve()), {"tipo_richiesta": "storica", "extra": "all", "captcha_free": True})
    ]
    assert calls["session"] == [
        {
            "batch": [batch_dir.resolve()],
            "minutes": 26,
            "logged_in_at": "14:05",
            "grace_seconds": 60,
            "port": 8027,
            "preflight_cf": None,
            "stop_stale_service": True,
            "cdp": None,
            "email_progress": False,
            "email_to": "demo@aecs4u.com",
            "dry_run": False,
            "keep_answered": False,
            "probe": False,
        }
    ]


def test_batch_run_list_dry_run_uses_and_removes_temporary_batch(monkeypatch, tmp_path):
    from sister.command_tools import build_batch_from_list, run_in_session, run_list

    built_dirs = []
    session_args = {}

    def fake_build(_source, output_dir, **_kwargs):
        built_dirs.append(output_dir)
        return 0

    def fake_session(**kwargs):
        session_args.update(kwargs)
        return 0

    monkeypatch.setattr(build_batch_from_list, "run", fake_build)
    monkeypatch.setattr(run_in_session, "run", fake_session)
    source, requested_dir = tmp_path / "list.md", tmp_path / "requested"
    source.write_text("# test\n", encoding="utf-8")
    result = run_list.run(list_file=source, batch_dir=requested_dir, dry_run=True)

    assert result == 0
    assert built_dirs and built_dirs[0] != requested_dir
    assert not built_dirs[0].exists()
    assert session_args["batch"] == built_dirs
    assert session_args["dry_run"] is True


def test_batch_run_list_cli_calls_run_in_process(monkeypatch, tmp_path):
    from sister.command_tools import run_list

    captured = {}
    monkeypatch.setattr(run_list, "run", lambda **kwargs: captured.update(kwargs) or 0)
    source, batch_dir = tmp_path / "list.md", tmp_path / "batch"
    result = runner.invoke(
        app,
        ["batch", "run-list", str(source), str(batch_dir), "--extra", "all", "--stop-stale-service", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert captured["list_file"] == source
    assert captured["batch_dir"] == batch_dir
    assert captured["extra"] == "all"
    assert captured["stop_stale_service"] is True
    assert captured["dry_run"] is True


def test_batch_convert_list_legacy_output_warns_and_forwards(monkeypatch, tmp_path):
    from sister.command_tools import convert_code_list

    captured = {}
    monkeypatch.setattr(
        convert_code_list, "run", lambda *args, **kwargs: captured.update(args=args, kwargs=kwargs) or 0
    )
    source, destination = tmp_path / "legacy.txt", tmp_path / "converted.md"
    result = runner.invoke(app, ["batch", "convert-list", str(source), "--output", str(destination)])

    assert result.exit_code == 0, result.output
    assert "--output è deprecato" in result.stderr
    assert captured == {"args": (source,), "kwargs": {"output": destination, "apply": False}}


@pytest.mark.parametrize(
    ("command", "module_name", "expected_dry_run", "options"),
    [
        ("prune-list", "prune_code_list", True, []),
        ("prune-list", "prune_code_list", False, ["--apply"]),
        ("convert-list", "convert_code_list", False, []),
        ("convert-list", "convert_code_list", True, ["--apply"]),
    ],
)
def test_batch_list_tools_use_in_process_run_with_safe_defaults(
    monkeypatch, tmp_path, command, module_name, expected_dry_run, options
):
    from sister.command_tools import convert_code_list, prune_code_list

    module = {"prune_code_list": prune_code_list, "convert_code_list": convert_code_list}[module_name]
    captured = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(module, "run", fake_run)
    list_file = tmp_path / "list.md"
    result = runner.invoke(app, ["batch", command, str(list_file), *options])

    assert result.exit_code == 0, result.output
    assert captured["args"] == (list_file,)
    if command == "prune-list":
        assert captured["dry_run"] is expected_dry_run
    else:
        assert captured["apply"] is expected_dry_run


def test_batch_convert_pvp_csv_uses_in_process_run_function(monkeypatch, tmp_path):
    from sister.command_tools import convert_pvp_sales_csvs_to_markdown

    calls = []
    monkeypatch.setattr(convert_pvp_sales_csvs_to_markdown, "run", lambda *args: calls.append(args) or 0)
    source_dir, output_dir = tmp_path / "source", tmp_path / "output"
    result = runner.invoke(app, ["batch", "convert-pvp-csv", str(source_dir), str(output_dir)])

    assert result.exit_code == 0, result.output
    assert calls == [(source_dir, output_dir)]


def test_app_login_uses_in_process_async_run(monkeypatch):
    from sister.command_tools import ade_login

    calls = []

    async def fake_run(*, close_only):
        calls.append(close_only)
        return 0

    monkeypatch.setattr(ade_login, "run", fake_run)
    result = runner.invoke(app, ["app", "login", "--close"])

    assert result.exit_code == 0, result.output
    assert calls == [True]


def test_app_hash_password_uses_in_process_run(monkeypatch):
    from sister.command_tools import hash_local_password

    calls = []
    prompts = iter(["secret", "secret"])
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(prompts))
    monkeypatch.setattr(
        hash_local_password,
        "run",
        lambda *args: calls.append(args) or (0, "user    hashed-value"),
    )
    result = runner.invoke(app, ["app", "hash-password", "user"])

    assert result.exit_code == 0, result.output
    assert calls == [("user", "secret", "secret")]
    assert "user    hashed-value" in result.output


def test_app_start_passes_typed_options_to_start_wrapper(monkeypatch, tmp_path):
    from sister.cli import app_tools

    calls = []
    monkeypatch.setattr(app_tools._runtime, "project_root", lambda: tmp_path)
    monkeypatch.setattr(
        app_tools.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or SimpleNamespace(returncode=0),
    )
    result = runner.invoke(app, ["app", "start", "--port", "8123", "--no-reload"])

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            ["bash", str(tmp_path / "scripts" / "start.sh"), "--port", "8123", "--no-reload"],
            {"cwd": tmp_path, "check": False},
        )
    ]


def test_app_stop_command_calls_guarded_stop_function(monkeypatch):
    from sister.cli import app_tools

    calls = []
    monkeypatch.setattr(app_tools, "stop_server", lambda port, *, pid: calls.append((port, pid)) or 0)
    result = runner.invoke(app, ["app", "stop", "8123", "--pid", "1234"])

    assert result.exit_code == 0, result.output
    assert calls == [(8123, 1234)]


def test_app_stop_refuses_listener_that_is_not_sister(monkeypatch):
    from sister.cli import app_tools

    calls = []
    monkeypatch.setattr(app_tools, "_listening_processes", lambda port, _proc_root: {1234: False})
    monkeypatch.setattr(app_tools.os, "kill", lambda *args: calls.append(args))
    result = app_tools.stop_server(8025)

    assert result == 1
    assert calls == []


@pytest.mark.parametrize(
    ("command", "module_name", "options", "expected"),
    [
        ("normalize", "normalize_documents", [], {"apply": False}),
        ("normalize", "normalize_documents", ["--apply", "--only", "old.pdf"], {"apply": True, "only": ["old.pdf"]}),
        ("reconcile", "reconcile_missing_documents", ["--apply"], {"apply": True}),
        ("cleanup-xml", "cleanup_extracted_xml", ["--skip-db"], {"apply": False, "skip_db": True}),
        ("rename-date", "rename_documents_with_date", ["--revert", "plan.json"], {"apply": False}),
    ],
)
def test_document_maintenance_commands_call_run_with_dry_run_default(
    monkeypatch, tmp_path, command, module_name, options, expected
):
    from sister.command_tools import (
        cleanup_extracted_xml,
        normalize_documents,
        reconcile_missing_documents,
        rename_documents_with_date,
    )

    modules = {
        "normalize_documents": normalize_documents,
        "reconcile_missing_documents": reconcile_missing_documents,
        "cleanup_extracted_xml": cleanup_extracted_xml,
        "rename_documents_with_date": rename_documents_with_date,
    }
    captured = {}
    monkeypatch.setattr(modules[module_name], "run", lambda **kwargs: captured.update(kwargs) or 0)
    documents = tmp_path / "documents"
    result = runner.invoke(app, ["documents", command, "--documents", str(documents), *options])

    assert result.exit_code == 0, result.output
    assert captured["documents"] == documents
    for key, value in expected.items():
        assert captured[key] == value


def test_documents_import_calls_async_run_in_process(monkeypatch, tmp_path):
    from sister import database
    from sister.command_tools import import_documents

    calls = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return 0

    async def fake_init_db():
        return None

    monkeypatch.setattr(import_documents, "run", fake_run)
    monkeypatch.setattr(database, "init_db", fake_init_db)
    documents = tmp_path / "documents"
    result = runner.invoke(app, ["documents", "import", "--documents", str(documents)])

    assert result.exit_code == 0, result.output
    assert calls == [{"documents": documents}]


def test_documents_import_ocular_calls_run_in_process(monkeypatch, tmp_path):
    from sister.command_tools import import_ocular_extraction

    calls = []
    monkeypatch.setattr(import_ocular_extraction, "run", lambda *args: calls.append(args) or 0)
    structured = tmp_path / "structured.json"
    structured.write_text("{}", encoding="utf-8")
    result = runner.invoke(app, ["documents", "import-ocular", "42", str(structured)])

    assert result.exit_code == 0, result.output
    assert calls == [(42, structured)]


@pytest.mark.parametrize(
    ("command", "module_name", "options", "expected"),
    [
        (
            "smart-ocr",
            "process_sister_pdf_smart_ocr",
            ["--output-root", "out", "--workers", "3"],
            {"output_root": Path("out"), "workers": 3},
        ),
        (
            "download-missing-p7m",
            "download_missing_p7m",
            ["--documents", "docs", "--sister-url", "http://sister.test"],
            {"documents": Path("docs"), "base_url": "http://sister.test"},
        ),
    ],
)
def test_documents_ocr_and_p7m_tools_call_run_in_process(monkeypatch, command, module_name, options, expected):
    from sister.command_tools import download_missing_p7m, process_sister_pdf_smart_ocr

    modules = {
        "process_sister_pdf_smart_ocr": process_sister_pdf_smart_ocr,
        "download_missing_p7m": download_missing_p7m,
    }
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return (0, {"results": []}) if module_name == "download_missing_p7m" else 0

    monkeypatch.setattr(modules[module_name], "run", fake_run)
    result = runner.invoke(app, ["documents", command, *options])

    assert result.exit_code == 0, result.output
    if module_name == "download_missing_p7m":
        expected = {**expected, "return_report": True}
    assert captured == expected


def test_documents_define_workflow_calls_run_in_process(monkeypatch, tmp_path):
    from sister.command_tools import define_document_workflow_batch

    captured = {}
    monkeypatch.setattr(
        define_document_workflow_batch,
        "run",
        lambda **kwargs: captured.update(kwargs) or (0, {"manifest": str(kwargs["output"])}),
    )
    documents, output = tmp_path / "docs", tmp_path / "manifest.jsonl"
    result = runner.invoke(
        app,
        ["documents", "define-workflow", "--documents", str(documents), "--manifest", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert captured == {"documents": documents, "output": output, "return_report": True}


def test_documents_define_workflow_legacy_output_warns_and_forwards(monkeypatch, tmp_path):
    from sister.command_tools import define_document_workflow_batch

    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0, {"manifest": str(kwargs["output"]), "jobs": 0}

    monkeypatch.setattr(define_document_workflow_batch, "run", fake_run)
    documents, manifest = tmp_path / "documents", tmp_path / "manifest.jsonl"
    result = runner.invoke(
        app,
        ["documents", "define-workflow", "--documents", str(documents), "--output", str(manifest)],
    )

    assert result.exit_code == 0, result.output
    assert "--output è deprecato" in result.stderr
    assert captured == {"documents": documents, "output": manifest, "return_report": True}


def test_build_dossier_graph_calls_run_in_process(monkeypatch, tmp_path):
    from sister.command_tools import build_dossier_graph

    captured = {}
    monkeypatch.setattr(build_dossier_graph, "run", lambda **kwargs: captured.update(kwargs) or 0)
    batch_dir, graph_file = tmp_path / "batch", tmp_path / "graph.json"
    result = runner.invoke(
        app,
        [
            "build",
            "dossier-graph",
            "--seed",
            "RSSMRA85M01H501Z",
            "--from-batch",
            str(batch_dir),
            "--out",
            str(graph_file),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "seeds": ["RSSMRA85M01H501Z"],
        "from_batch": batch_dir,
        "resume": None,
        "out": graph_file,
        "max_depth": 0,
        "max_nodes": 0,
        "retry_errors": False,
        "max_requests": 300,
    }


def test_build_building_kmz_calls_run_in_process(monkeypatch, tmp_path):
    from sister.command_tools import build_building_kmz

    captured = {}
    monkeypatch.setattr(build_building_kmz, "run", lambda **kwargs: captured.update(kwargs) or 0)
    osm_json, out = tmp_path / "osm.json", tmp_path / "building.kmz"
    result = runner.invoke(
        app,
        [
            "build",
            "building-kmz",
            "--lat",
            "44.4",
            "--lng",
            "12.2",
            "--osm-json",
            str(osm_json),
            "--out",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "lat": 44.4,
        "lng": 12.2,
        "radius": 80,
        "name": "Edificio",
        "nearest": 3,
        "osm_json": osm_json,
        "out": out,
    }


def test_batch_build_pdf_parseable_calls_run_in_process(monkeypatch, tmp_path):
    from sister.command_tools import build_pdf_parseable_batch

    captured = {}
    monkeypatch.setattr(build_pdf_parseable_batch, "run", lambda **kwargs: captured.update(kwargs) or 0)
    documents_dir, batch_csv, inventory_csv = tmp_path / "documents", tmp_path / "batch.csv", tmp_path / "inventory.csv"
    result = runner.invoke(
        app,
        [
            "batch",
            "build-pdf-parseable",
            "--documents-dir",
            str(documents_dir),
            "--batch-csv",
            str(batch_csv),
            "--inventory-csv",
            str(inventory_csv),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {"documents_dir": documents_dir, "batch_csv": batch_csv, "inventory_csv": inventory_csv}


def test_batch_recrowd_calls_in_process_run_and_forwards_variant_options(monkeypatch):
    from sister.command_tools import recrowd_storica_analitica

    captured = {}
    monkeypatch.setattr(
        recrowd_storica_analitica,
        "run",
        lambda items, *, forwarded: captured.update(items=items, forwarded=forwarded) or 0,
    )
    result = runner.invoke(app, ["batch", "recrowd", "--items", "proponents", "--limit", "2"])

    assert result.exit_code == 0, result.output
    assert captured == {"items": "proponents", "forwarded": ["--limit", "2"]}


def test_batch_run_pvp_is_calls_in_process_runner(monkeypatch, tmp_path):
    from sister.command_tools import run_pvp_is

    captured = {}
    monkeypatch.setattr(run_pvp_is, "run", lambda *args: captured.update(args=args) or 0)
    source, batch_dir = tmp_path / "IS.md", tmp_path / "batch"
    result = runner.invoke(app, ["batch", "run-pvp-is", str(source), str(batch_dir)])

    assert result.exit_code == 0, result.output
    assert captured == {"args": (source, batch_dir)}


def test_batch_run_pdf_parseable_calls_in_process_runner(monkeypatch, tmp_path):
    from sister.command_tools import run_pdf_parseable_batch

    captured = {}
    monkeypatch.setattr(run_pdf_parseable_batch, "run", lambda **kwargs: captured.update(kwargs) or 0)
    docs, batch_csv, inventory, output = (tmp_path / name for name in ("docs", "batch.csv", "inventory.csv", "out"))
    result = runner.invoke(
        app,
        [
            "batch",
            "run-pdf-parseable",
            "--documents-dir",
            str(docs),
            "--batch-csv",
            str(batch_csv),
            "--inventory-csv",
            str(inventory),
            "--output-dir",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {"documents_dir": docs, "batch_csv": batch_csv, "inventory_csv": inventory, "output_dir": output}


@pytest.mark.parametrize(
    ("command", "script_name", "args"),
    [
        ("sweep", "sweep", ["demo@aecs4u.com"]),
        ("interactions", "interactions", None),
        ("demo-purchases", "demo_purchases", None),
        ("purchase-paths", "purchase_paths", None),
    ],
)
def test_audit_commands_use_in_process_runner(monkeypatch, command, script_name, args):
    from sister.command_tools.ui_audit import runner as audit_runner

    calls = []
    monkeypatch.setattr(audit_runner, "run_audit", lambda name, *, argv=None: calls.append((name, argv)) or 0)
    cli_args = ["audit", command]
    if command == "sweep":
        cli_args.append("demo@aecs4u.com")
    result = runner.invoke(app, cli_args)

    assert result.exit_code == 0, result.output
    assert calls == [(script_name, args)]


def test_ui_audit_runner_restores_process_state_and_maps_failed_checks(monkeypatch, tmp_path):
    from sister.command_tools.ui_audit import runner as audit_runner

    monkeypatch.setattr(audit_runner, "project_root", lambda: tmp_path)
    monkeypatch.setattr(audit_runner.runpy, "run_path", lambda *_args, **_kwargs: {"res": {"check": False}})
    argv_before, path_before, cwd_before = sys.argv, list(sys.path), Path.cwd()
    result = audit_runner.run_audit("interactions", argv=["example"])

    assert result == 1
    assert sys.argv is argv_before
    assert sys.path == path_before
    assert Path.cwd() == cwd_before


def test_run_pdf_parseable_reuses_the_query_batch_callback(monkeypatch, tmp_path):
    from sister.cli import query as query_cli
    from sister.command_tools import build_pdf_parseable_batch, run_pdf_parseable_batch

    calls = {"build": [], "batch": []}
    monkeypatch.setattr(build_pdf_parseable_batch, "run", lambda **kwargs: calls["build"].append(kwargs) or 0)
    monkeypatch.setattr(query_cli, "batch", lambda **kwargs: calls["batch"].append(kwargs))
    csv_path, inventory, output = tmp_path / "batch.csv", tmp_path / "inventory.csv", tmp_path / "output"
    result = run_pdf_parseable_batch.run(batch_csv=csv_path, inventory_csv=inventory, output_dir=output)

    assert result == 0
    assert calls["build"] == [
        {
            "documents_dir": build_pdf_parseable_batch.DEFAULT_DOCUMENTS,
            "batch_csv": csv_path,
            "inventory_csv": inventory,
        }
    ]
    assert calls["batch"][0]["input_file"] == str(csv_path)
    assert calls["batch"][0]["command"] == "auto"
    assert calls["batch"][0]["wait"] is True
    assert calls["batch"][0]["output_dir"] == str(output)


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

    from sister.client import VisuraClient

    async def offline(self):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(VisuraClient, "health", offline)
    result = runner.invoke(app, ["health"])

    assert result.exit_code == 3
    assert "Servizio non raggiungibile" in result.output


@pytest.mark.parametrize(
    ("command_args", "search_type", "expected"),
    [
        (
            ["query", "ipotecaria-immobile", "-P", "Trieste", "-C", "TRIESTE", "-F", "9", "-p", "166", "-t", "F"],
            "immobile",
            {"provincia": "Trieste", "comune": "TRIESTE", "foglio": "9", "particella": "166", "tipo_catasto": "F"},
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
    ids=["immobile", "azienda", "nota"],
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
    assert "Solo anteprima" in result.output
    assert "flusso admin" in result.output
    assert "autenticato" in result.output
    assert "non autorizza pagamenti" in result.output
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
        [
            "query",
            "ipotecaria-immobile",
            "-P",
            "Trieste",
            "-C",
            "TRIESTE",
            "-F",
            "9",
            "-p",
            "166",
            "--yes",
            "--wait",
            "-o",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "la vecchia opzione --yes/-y viene ignorata" in result.stderr
    assert calls[0]["auto_confirm"] is False
    assert calls[1] == {"waited_for": "inspection-yes"}
    assert "Costo: Costo (€1.25)" in result.output
    assert "Costo non confermato" in result.output
    assert json.loads(output.read_text()) == {
        "request_id": "inspection-yes",
        "status": "completed",
        "data": {"cost": {"text": "Costo", "value": 1.25}, "confirmed": False},
    }


def test_paid_inspection_help_hides_legacy_yes_flag():
    result = runner.invoke(app, ["query", "ipotecaria-immobile", "--help"])

    assert result.exit_code == 0, result.output
    assert "--yes" not in result.output
    assert "-y" not in result.output


@pytest.mark.parametrize("command", ["ipotecaria-immobile", "ipotecaria-azienda", "ipotecaria-nota"])
def test_paid_query_help_and_dry_run_are_marked(command):
    help_result = runner.invoke(app, ["query", command, "--help"])
    assert help_result.exit_code == 0
    help_header = help_result.output.split("╭─ Options", 1)[0]
    assert any(line.lstrip().startswith("SERVIZIO A PAGAMENTO") for line in help_header.splitlines())

    args_by_command = {
        "ipotecaria-immobile": ["-P", "Roma"],
        "ipotecaria-azienda": ["-P", "Roma", "--id", "02471840997"],
        "ipotecaria-nota": ["-P", "Roma", "--numero", "123", "--anno", "2024"],
    }
    dry_run = runner.invoke(app, ["query", command, *args_by_command[command], "--dry-run"])
    assert dry_run.exit_code == 0, dry_run.output
    assert "SERVIZIO A PAGAMENTO" in dry_run.output


def test_legacy_ipotecaria_persona_alias_uses_free_preview(monkeypatch):
    from sister.client import VisuraClient

    calls = []

    async def fake_preview(self, **kwargs):
        calls.append(kwargs)
        return {"request_id": "preview-1", "status": "queued"}

    monkeypatch.setattr(VisuraClient, "mortgage_search_start", fake_preview)
    result = runner.invoke(app, ["query", "ipotecaria-persona", "-P", "Trieste", "--cf", "RSSMRA85M01H501Z"])

    assert result.exit_code == 0, result.output
    assert "è deprecato" in result.stderr
    assert calls == [
        {"provincia": "Trieste", "codice_fiscale": "RSSMRA85M01H501Z", "form_fields": None, "force": False}
    ]


def test_mortgage_start_is_a_deprecated_alias(monkeypatch):
    from sister.client import VisuraClient

    calls = []

    async def fake_preview(self, **kwargs):
        calls.append(kwargs)
        return {"request_id": "preview-2", "status": "queued"}

    monkeypatch.setattr(VisuraClient, "mortgage_search_start", fake_preview)
    result = runner.invoke(app, ["mortgage", "start", "-P", "Trieste", "--cf", "RSSMRA85M01H501Z"])

    assert result.exit_code == 0, result.output
    assert "è deprecato" in result.stderr
    assert calls[0]["provincia"] == "Trieste"
    assert calls[0]["codice_fiscale"] == "RSSMRA85M01H501Z"


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
            "workflow",
            "run",
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


def test_workflow_run_custom_dry_run_shows_server_step_plan():
    result = runner.invoke(
        app,
        [
            "workflow",
            "run",
            "-P",
            "Trieste",
            "-C",
            "TRIESTE",
            "-F",
            "9",
            "-p",
            "166",
            "--with",
            "mappa",
            "--with",
            "fiduciali",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "preset=custom" in result.output
    assert "Workflow personalizzato" in result.output
    assert "search → intestati → mappa → fiduciali" in result.output


def test_deprecated_query_workflow_alias_calls_new_command(monkeypatch):
    from sister.client import VisuraClient

    calls = []

    async def fake_workflow(self, **kwargs):
        calls.append(kwargs)
        return {"workflow_id": "wf_alias", "steps": [], "summary": {"completed": 0}}

    monkeypatch.setattr(VisuraClient, "workflow", fake_workflow)
    result = runner.invoke(
        app,
        ["query", "workflow", "-P", "Roma", "-C", "ROMA", "-F", "1", "-p", "2"],
    )

    assert result.exit_code == 0, result.output
    assert "è deprecato; usa `sister workflow run`" in result.stderr
    assert calls[0]["preset"] == "custom"
    assert calls[0]["custom_steps"] == ["search", "intestati"]


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
    assert "Usa --confirm-paid" in result.output
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
    assert "--depth deep con --include-paid richiede --confirm-paid" in _plain(result.output)
    assert calls == []


def test_workflow_help_hides_legacy_paid_confirmation_flag():
    result = runner.invoke(app, ["query", "workflow", "--help"])

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "--confirm-paid" in output
    assert "--yes" not in output


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
            "workflow",
            "run",
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
    assert "immobili: 2 elementi" in result.output
    assert "total: 2" in result.output
    assert "Risultati troncati" in result.output
    assert "owner lookup failed" in result.output
    assert "Indicatori di rischio: 3" in result.output
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

    assert result.exit_code == 2
    assert "missing parcel" in result.output


def test_dev_fixture_payloads_forwards_unknown_arguments(monkeypatch, tmp_path):
    from sister.cli import dev

    monkeypatch.setattr(dev._runtime, "project_root", lambda: tmp_path)
    calls = []

    def fake_run(command, *, cwd, check):
        calls.append((command, cwd, check))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    result = runner.invoke(app, ["dev", "fixture-payloads", "--example-option", "value"])

    expected = ["bash", str(tmp_path / "scripts" / "sister_fixture_payload_commands.sh"), "--example-option", "value"]
    assert result.exit_code == 7
    assert calls == [(expected, tmp_path, False)]


def test_app_start_converts_keyboard_interrupt_to_exit_130(monkeypatch, tmp_path):
    from sister.cli import app_tools

    monkeypatch.setattr(app_tools._runtime, "project_root", lambda: tmp_path)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(app_tools.subprocess, "run", interrupt)
    result = runner.invoke(app, ["app", "start"])

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
    assert "migrazioni applicate" in result.output.lower()
    if command_name == "init":
        assert "è deprecato; usa `sister db migrate`" in result.stderr


def test_db_status_reports_current_migration(monkeypatch):
    import asyncio

    from sister.cli import db

    status = {
        "current": ["rev_1"],
        "heads": ["rev_1"],
        "database_revision": "rev_1",
        "revision_matches": True,
        "behind": False,
    }

    async def fake_status():
        return status

    calls = []

    def fake_run_db(coro_factory, *, initialize=True):
        calls.append(initialize)
        return asyncio.run(coro_factory())

    monkeypatch.setattr(db, "_migration_status", fake_status)
    monkeypatch.setattr(db._runtime, "run_db", fake_run_db)

    result = runner.invoke(app, ["db", "status"])

    assert result.exit_code == 0, result.output
    assert calls == [False]
    assert "DATABASE_REVISION: rev_1" in result.output
    assert "Applicate: rev_1 | Teste: rev_1 | aggiornato" in result.output


def test_db_status_reports_database_behind_heads(monkeypatch):
    import asyncio

    from sister.cli import db

    async def fake_status():
        return {
            "current": ["rev_old"],
            "heads": ["rev_new"],
            "database_revision": "rev_new",
            "revision_matches": False,
            "behind": True,
        }

    monkeypatch.setattr(db, "_migration_status", fake_status)
    monkeypatch.setattr(db._runtime, "run_db", lambda coro_factory, **_kwargs: asyncio.run(coro_factory()))

    result = runner.invoke(app, ["db", "status"])

    assert result.exit_code == 1
    assert "Applicate: rev_old | Teste: rev_new | indietro" in result.output


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

    monkeypatch.setattr(
        database, "backfill_ownership_history", fake_history
    )  # the real one wipes and rebuilds the live history
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
        "mortgage_searches": {
            "responses": 2,
            "searches": 2,
            "formalities": 4,
            "annotations": 1,
            "document_requests": 0,
        },
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
            "documents",
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
    assert "Esito validazione: match" in result.output
    assert json.loads(output.read_text()) == report


def test_floor_plan_validate_reports_invalid_json(tmp_path):
    visura = tmp_path / "visura.json"
    visura.write_text("not-json")

    result = runner.invoke(
        app,
        ["documents", "floor-plan-validate", "--visura", str(visura), "--floor-plan", str(tmp_path / "plan.pdf")],
    )

    assert result.exit_code == 1
    assert "Dati di input non validi per la planimetria" in result.output


def test_floor_plan_root_alias_warns_and_forwards(tmp_path):
    visura = tmp_path / "visura.json"
    visura.write_text("not-json")

    result = runner.invoke(
        app,
        ["floor-plan-validate", "--visura", str(visura), "--floor-plan", str(tmp_path / "plan.pdf")],
    )

    assert result.exit_code == 1
    assert "è deprecato; usa `sister documents floor-plan-validate`" in result.stderr
    assert "Dati di input non validi per la planimetria" in result.output
