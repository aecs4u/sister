"""Tests for shared CLI output helpers."""

import json
from io import StringIO

import pytest
import typer
from rich.console import Console
from rich.text import Text

from sister.cli import _runtime


def test_db_alembic_config_uses_checkout_path_for_non_editable_install(monkeypatch, tmp_path):
    from sister.cli import db

    checkout = tmp_path / "sister-checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\nname = 'sister'\n", encoding="utf-8")
    (checkout / "alembic.ini").write_text("[alembic]\nscript_location = migrations\n", encoding="utf-8")
    installed_runtime = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "sister" / "cli" / "_runtime.py"
    monkeypatch.setattr(_runtime, "__file__", str(installed_runtime))
    monkeypatch.setenv("SISTER_HOME", str(checkout))

    config = db._alembic_config()

    assert config.config_file_name == str(checkout / "alembic.ini")


def test_emit_writes_json_to_stdout_and_file_notices_to_stderr(tmp_path, monkeypatch):
    stdout = StringIO()
    stderr = StringIO()
    import typer

    def echo(message, *, err=False, **_kwargs):
        stream = stderr if err else stdout
        stream.write(f"{message}\n")

    monkeypatch.setattr(typer, "echo", echo)
    monkeypatch.setattr(_runtime, "console", Console(file=stdout, width=200, force_terminal=False))
    output_path = tmp_path / "nested" / "result.json"
    result = {"status": "completed", "value": 3}

    _runtime.emit(result, output=output_path, fmt=_runtime.OutputFormat.JSON)

    assert json.loads(stdout.getvalue()) == result
    assert "File di output scritto in" not in stdout.getvalue()
    assert "File di output scritto in" in stderr.getvalue()
    assert json.loads(output_path.read_text(encoding="utf-8")) == result


def test_emit_uses_the_supplied_rich_renderer(monkeypatch):
    stdout = StringIO()
    monkeypatch.setattr(_runtime, "console", Console(file=stdout, width=200, force_terminal=False))

    _runtime.emit({"message": "render me"}, fmt=_runtime.OutputFormat.RICH, render=lambda data: Text(data["message"]))

    assert stdout.getvalue().strip() == "render me"


@pytest.mark.parametrize("status", ["error", "expired", "needs_human"])
def test_exit_for_status_uses_failure_exit_code(status):
    with pytest.raises(typer.Exit) as raised:
        _runtime.exit_for_status({"status": "completed"}, {"status": status})

    assert raised.value.exit_code == 1
