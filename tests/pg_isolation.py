"""Isolated PostgreSQL schema for the database-backed tests.

``sister.database`` is PostgreSQL-only, so DB tests need a real Postgres. They never touch the application's own schema:
a session-scoped throwaway schema ``sister_test_<8 hex>`` is created on the *local* server named by ``DATABASE_DSN``,
filled from the current ORM models, stamped with the expected Alembic revision, truncated before every test and dropped
at the end. Safety rules, enforced in code:

* the DSN host must be local (127.0.0.1 / localhost / ::1) — never a remote or cloud database;
* only schemas matching ``^sister_test_[0-9a-f]{8}$`` are ever created, swept or dropped;
* the schema the application uses (``search_path`` in the DSN) is never written to.

If no local PostgreSQL is configured/reachable the DB tests are skipped with that reason.
"""

from __future__ import annotations

import os
import re
import uuid

import pytest

_SCHEMA_RE = re.compile(r"^sister_test_[0-9a-f]{8}$")
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _local_dsn():
    from sqlalchemy.engine import make_url

    dsn = os.getenv("DATABASE_DSN")
    if not dsn:
        pytest.skip("DB tests need DATABASE_DSN pointing at a local PostgreSQL")
    url = make_url(dsn)
    if url.get_backend_name() != "postgresql":
        pytest.skip("DB tests need a PostgreSQL DATABASE_DSN")
    if (url.host or "") not in _LOCAL_HOSTS:
        pytest.skip(f"DB tests only run against a local PostgreSQL (DATABASE_DSN host is {url.host!r})")
    return url


def _drop(admin, schema: str) -> None:
    from sqlalchemy import text

    assert _SCHEMA_RE.fullmatch(schema), f"refusing to drop {schema!r}"
    with admin.connect() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def _apply_table_migrations(engine) -> None:
    """Create the tables that exist only as Alembic migrations (no ORM model), by running those migrations' DDL."""
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20260925_add_sezioni_extraction_jobs.py"
    spec = importlib.util.spec_from_file_location("_sezioni_jobs_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()


@pytest.fixture(scope="session")
def pg_test_schema():
    """Create the throwaway schema once; yields ``(schema_name, async_dsn_with_search_path)``."""
    from sqlalchemy import create_engine, text
    from sqlmodel import SQLModel

    import sister.cadastral  # noqa: F401  (registers the ORM models on SQLModel.metadata)
    import sister.database as database
    import sister.db_models  # noqa: F401
    import sister.visura_xml_models  # noqa: F401

    url = _local_dsn()
    sync_url = url.set(drivername="postgresql+psycopg")
    admin = create_engine(sync_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text("SELECT 1"))
            orphans = conn.execute(
                text("SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'sister\\_test\\_%'")
            ).scalars().all()
    except Exception as exc:  # server down, bad credentials, ...
        admin.dispose()
        pytest.skip(f"PostgreSQL not reachable for DB tests: {type(exc).__name__}")

    for orphan in orphans:  # leftovers of a crashed earlier run
        if _SCHEMA_RE.fullmatch(orphan):
            _drop(admin, orphan)

    schema = "sister_test_" + uuid.uuid4().hex[:8]
    assert _SCHEMA_RE.fullmatch(schema)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        query = dict(url.query)
        query["options"] = f"-csearch_path={schema}"
        schema_sync_url = sync_url.set(query=query)
        engine = create_engine(schema_sync_url)
        SQLModel.metadata.create_all(engine)
        _apply_table_migrations(engine)
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num varchar(64) NOT NULL PRIMARY KEY)"))
            conn.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": database.DATABASE_REVISION})
        engine.dispose()
        yield schema, url.set(query=query).render_as_string(hide_password=False), schema_sync_url
    finally:
        _drop(admin, schema)
        admin.dispose()


@pytest.fixture
async def fresh_db(pg_test_schema, monkeypatch):
    """Point ``sister.database`` at the throwaway schema with every table empty."""
    from sqlalchemy import create_engine, text

    import sister.database as database

    schema, async_dsn, sync_url = pg_test_schema
    assert _SCHEMA_RE.fullmatch(schema)
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            tables = conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = :s AND tablename <> 'alembic_version'"),
                {"s": schema},
            ).scalars().all()
            if tables:
                conn.execute(text("TRUNCATE " + ", ".join(f'"{schema}"."{t}"' for t in tables) + " RESTART IDENTITY CASCADE"))
    finally:
        engine.dispose()

    monkeypatch.setattr(database, "DATABASE_DSN", async_dsn)
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_db_writable", None)
    yield
    if database._engine is not None:
        await database._engine.dispose()
        database._engine = None
