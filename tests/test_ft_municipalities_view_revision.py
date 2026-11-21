"""``20261212_ft_municipalities_view`` makes a plain shared ISTAT table readable as ``ft_municipalities``."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

REVISION = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20261212_ft_municipalities_view.py"


def _run(engine, schema: str, step: str) -> None:
    spec = importlib.util.spec_from_file_location("ft_municipalities_view_revision", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn, opts={"istat_schema": schema})):
            getattr(module, step)()


@pytest.fixture
def istat_schema(pg_test_schema):
    _schema, _async_dsn, sync_url = pg_test_schema
    engine = create_engine(sync_url)
    name = "sister_test_ft_" + uuid.uuid4().hex[:8]
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{name}"'))
    try:
        yield engine, name
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        engine.dispose()


def test_plain_table_gets_a_view_and_upgrade_is_repeatable(istat_schema):
    engine, schema = istat_schema
    with engine.begin() as conn:
        conn.execute(text(f'CREATE TABLE "{schema}".municipalities (numeric_code int PRIMARY KEY, name text)'))
        conn.execute(text(f"INSERT INTO \"{schema}\".municipalities VALUES (58091, 'Roma')"))
    _run(engine, schema, "upgrade")
    _run(engine, schema, "upgrade")
    with engine.connect() as conn:
        assert conn.execute(text(f'SELECT name FROM "{schema}".ft_municipalities')).scalar_one() == "Roma"
        listed = conn.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_schema = :s AND table_name = 'ft_municipalities'"),
            {"s": schema},
        ).scalar_one_or_none()
    assert listed == 1  # what init_db checks
    _run(engine, schema, "downgrade")
    with engine.connect() as conn:
        assert conn.execute(text(f'SELECT count(*) FROM "{schema}".municipalities')).scalar_one() == 1  # data untouched


def test_existing_ft_municipalities_is_left_alone(istat_schema):
    engine, schema = istat_schema
    with engine.begin() as conn:
        conn.execute(text(f'CREATE TABLE "{schema}".ft_municipalities (numeric_code int PRIMARY KEY)'))
    _run(engine, schema, "upgrade")
    with engine.connect() as conn:
        kind = conn.execute(text(f"SELECT relkind FROM pg_class WHERE oid = '\"{schema}\".ft_municipalities'::regclass")).scalar_one()
    assert kind == "r"
    _run(engine, schema, "downgrade")  # never drops something that is not our view


def test_missing_istat_table_fails_loudly(istat_schema):
    engine, schema = istat_schema
    with pytest.raises(RuntimeError, match="was not found"):
        _run(engine, schema, "upgrade")
