import pytest

from sister.database_config import database_dsn_setting, get_database_dsn


@pytest.mark.parametrize(
    ("environment", "suffix"),
    [
        ("development", "LOCALHOST"),
        ("local", "LOCALHOST"),
        ("localhost", "LOCALHOST"),
        ("test", "LOCALHOST"),
        ("testing", "LOCALHOST"),
        ("production", "NEON"),
        ("prod", "NEON"),
        ("staging", "NEON"),
        (" Production ", "NEON"),
    ],
)
def test_database_selection_follows_environment(monkeypatch, environment, suffix):
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("SISTER_DATABASE_DSN_LOCALHOST", "postgresql://localhost/sister")
    monkeypatch.setenv("SISTER_DATABASE_DSN_NEON", "postgresql://example.invalid/sister")
    monkeypatch.setenv("DATABASE_DSN", "postgresql://old.invalid/unrelated")

    assert database_dsn_setting() == f"SISTER_DATABASE_DSN_{suffix}"
    expected = "postgresql://localhost/sister" if suffix == "LOCALHOST" else "postgresql://example.invalid/sister"
    assert get_database_dsn(required=True) == expected


def test_database_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("SISTER_DATABASE_DSN_LOCALHOST", " postgresql://localhost/sister ")
    assert get_database_dsn() == "postgresql://localhost/sister"


@pytest.mark.parametrize(("environment", "suffix"), [("development", "LOCALHOST"), ("production", "NEON")])
@pytest.mark.parametrize("missing_value", [None, "", "  "])
def test_missing_selected_database_never_falls_back(monkeypatch, environment, suffix, missing_value):
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("DATABASE_DSN", "postgresql://old.invalid/unrelated")
    monkeypatch.setenv("SISTER_DATABASE_DSN_LOCALHOST", "postgresql://localhost/sister")
    monkeypatch.setenv("SISTER_DATABASE_DSN_NEON", "postgresql://example.invalid/sister")
    selected = f"SISTER_DATABASE_DSN_{suffix}"
    if missing_value is None:
        monkeypatch.delenv(selected)
    else:
        monkeypatch.setenv(selected, missing_value)

    assert get_database_dsn() is None
    with pytest.raises(RuntimeError, match=selected):
        get_database_dsn(required=True)


def test_database_tests_always_select_the_local_setting(monkeypatch):
    from tests.pg_isolation import _local_dsn

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SISTER_DATABASE_DSN_LOCALHOST", "postgresql://postgres@127.0.0.1/sister")
    monkeypatch.setenv("SISTER_DATABASE_DSN_NEON", "postgresql://example.invalid/sister")
    assert _local_dsn().host == "127.0.0.1"


def test_database_tests_refuse_a_remote_local_setting(monkeypatch):
    from tests.pg_isolation import _local_dsn

    monkeypatch.setenv("SISTER_DATABASE_DSN_LOCALHOST", "postgresql://example.invalid/sister")
    with pytest.raises(pytest.skip.Exception, match="only run against a local PostgreSQL"):
        _local_dsn()
