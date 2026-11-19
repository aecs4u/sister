import pytest

from sister.database_config import database_dsn_setting, get_database_dsn


@pytest.mark.parametrize(
    ("environment", "suffix"),
    [
        ("development", "LOCALHOST"),
        ("local", "LOCALHOST"),
        ("localhost", "LOCALHOST"),
        ("test", "TEST"),
        ("testing", "TEST"),
        ("production", "NEON"),
        ("prod", "NEON"),
        ("staging", "NEON"),
        (" Production ", "NEON"),
    ],
)
def test_database_selection_follows_environment(monkeypatch, environment, suffix):
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("SISTER_DATABASE_DSN_LOCALHOST", "postgresql://localhost/sister")
    monkeypatch.setenv("SISTER_DATABASE_DSN_TEST", "postgresql://localhost/sister_test")
    monkeypatch.setenv("SISTER_DATABASE_DSN_NEON", "postgresql://example.invalid/sister")
    monkeypatch.setenv("DATABASE_DSN", "postgresql://old.invalid/unrelated")

    assert database_dsn_setting() == f"SISTER_DATABASE_DSN_{suffix}"
    expected = {
        "LOCALHOST": "postgresql://localhost/sister",
        "TEST": "postgresql://localhost/sister_test",
        "NEON": "postgresql://example.invalid/sister",
    }[suffix]
    assert get_database_dsn(required=True) == expected


def test_database_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("SISTER_DATABASE_DSN_LOCALHOST", " postgresql://localhost/sister ")
    assert get_database_dsn() == "postgresql://localhost/sister"


@pytest.mark.parametrize(
    ("environment", "suffix"),
    [("development", "LOCALHOST"), ("testing", "TEST"), ("production", "NEON")],
)
@pytest.mark.parametrize("missing_value", [None, "", "  "])
def test_missing_selected_database_never_falls_back(monkeypatch, environment, suffix, missing_value):
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("DATABASE_DSN", "postgresql://old.invalid/unrelated")
    monkeypatch.setenv("SISTER_DATABASE_DSN_LOCALHOST", "postgresql://localhost/sister")
    monkeypatch.setenv("SISTER_DATABASE_DSN_TEST", "postgresql://localhost/sister_test")
    monkeypatch.setenv("SISTER_DATABASE_DSN_NEON", "postgresql://example.invalid/sister")
    selected = f"SISTER_DATABASE_DSN_{suffix}"
    if missing_value is None:
        monkeypatch.delenv(selected)
    else:
        monkeypatch.setenv(selected, missing_value)

    assert get_database_dsn() is None
    with pytest.raises(RuntimeError, match=selected):
        get_database_dsn(required=True)


def test_database_tests_always_select_the_test_setting(monkeypatch):
    from tests.pg_isolation import _test_dsn

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SISTER_DATABASE_DSN_TEST", "postgresql://postgres@127.0.0.1/sister_test")
    monkeypatch.setenv("SISTER_DATABASE_DSN_NEON", "postgresql://example.invalid/sister")
    assert _test_dsn().database == "sister_test"


def test_database_tests_refuse_a_remote_test_setting(monkeypatch):
    from tests.pg_isolation import _test_dsn

    monkeypatch.setenv("SISTER_DATABASE_DSN_TEST", "postgresql://example.invalid/sister_test")
    with pytest.raises(pytest.skip.Exception, match="only run against a local PostgreSQL"):
        _test_dsn()
