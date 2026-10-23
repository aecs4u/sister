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

_SCHEMA_RE = re.compile(r"^sister_test_[0-9a-f]{8}(?:_(?:archive|istat))?$")
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


def _apply_table_migrations(engine, schema: str, istat_schema: str) -> None:
    """Create the tables that exist only as Alembic migrations (no ORM model), by running those migrations' DDL."""
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    migration_dir = Path(__file__).resolve().parent.parent / "alembic" / "versions"
    migrations = []
    for name in (
        "20260925_add_sezioni_extraction_jobs.py",
    ):
        path = migration_dir / name
        spec = importlib.util.spec_from_file_location(f"_{path.stem}_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migrations.append(migration)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn, opts={"sister_schema": schema, "istat_schema": istat_schema})):
            for migration in migrations:
                migration.upgrade()
        conn.execute(
            text(
                """CREATE TABLE sister_request_jobs (
                    request_id VARCHAR PRIMARY KEY,
                    request_type VARCHAR NOT NULL,
                    payload_json TEXT NOT NULL,
                    status VARCHAR NOT NULL DEFAULT 'queued',
                    error_kind VARCHAR,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    side_effect_started BOOLEAN NOT NULL DEFAULT FALSE,
                    lease_until TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )"""
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_sister_request_jobs_status_lease "
                "ON sister_request_jobs (status, lease_until)"
            )
        )
    migration_path = migration_dir / "20261028_istat_municipalities.py"
    spec = importlib.util.spec_from_file_location("_20261028_istat_municipalities_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn, opts={"sister_schema": schema, "istat_schema": istat_schema})):
            migration.upgrade()


def _apply_flat_schema(engine, schema: str, archive_schema: str, istat_schema: str) -> None:
    """Build the flat/normalised tables and views from the migration's own SQL, in the throwaway schemas.

    ``02_schema.sql`` and ``05_views.sql`` are the single definition of the layout (the ORM models only describe columns
    for queries), so a test schema cannot drift from what ``alembic upgrade`` creates. PostGIS lives in the application
    schema; where it is not installed the geometry column falls back to text so DB-layer tests still run.
    """
    import re as _re
    from pathlib import Path

    from sqlalchemy import text

    sql_dir = Path(__file__).resolve().parent.parent / "alembic" / "sql" / "20261024_flat_normalised"

    def render(name: str) -> str:
        sql = (sql_dir / name).read_text(encoding="utf-8")
        sql = _re.sub(r"^CREATE SCHEMA[^;]*;\n", "", sql, flags=_re.MULTILINE)  # the schemas exist already
        sql = _re.sub(r"^SET LOCAL search_path[^;]*;\n", "", sql, flags=_re.MULTILINE)
        return (
            sql.replace("sister_next", f'"{schema}"')
            .replace("sister_archive", f'"{archive_schema}"')
            .replace("istat.", f'"{istat_schema}".')
        )

    with engine.begin() as conn:
        postgis_schema = conn.execute(
            text("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'postgis'")
        ).scalar()
        schema_sql = render("02_schema.sql")
        if postgis_schema:
            conn.execute(text(f'SET LOCAL search_path = "{schema}", "{postgis_schema}"'))
        else:
            schema_sql = schema_sql.replace("geom geometry,", "geom text,")
            schema_sql = _re.sub(r"^CREATE INDEX ix_parcel_geometry_cache_geom[^;]*;\n", "", schema_sql, flags=_re.MULTILINE)
        conn.execute(text(schema_sql))
        conn.execute(text(render("05_views.sql")))


@pytest.fixture(scope="session")
def pg_test_schema():
    """Create the throwaway schema once; yields ``(schema_name, async_dsn_with_search_path)``."""
    from sqlalchemy import create_engine, text

    import sister.cadastral  # noqa: F401  (registers the ORM models on SQLModel.metadata)
    import sister.database as database
    import sister.db_models  # noqa: F401
    import sister.visura_xml_models  # noqa: F401

    url = _local_dsn()
    source_istat_schema = os.getenv("DATABASE_ISTAT_SCHEMA", "istat")
    sync_url = url.set(drivername="postgresql+psycopg")
    admin = create_engine(sync_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text("SELECT 1"))
            if conn.execute(
                text("SELECT to_regclass(:relation)"), {"relation": f'{source_istat_schema}.municipalities'}
            ).scalar_one() is None:
                pytest.skip(f"DB tests need {source_istat_schema}.municipalities for ISTAT references")
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
    archive_schema = schema + "_archive"
    istat_schema = schema + "_istat"
    assert _SCHEMA_RE.fullmatch(schema) and _SCHEMA_RE.fullmatch(archive_schema) and _SCHEMA_RE.fullmatch(istat_schema)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(text(f'CREATE SCHEMA "{archive_schema}"'))
        conn.execute(text(f'CREATE SCHEMA "{istat_schema}"'))
        source = admin.dialect.identifier_preparer.quote_schema(source_istat_schema)
        conn.execute(
            text(
                f'CREATE TABLE "{istat_schema}".municipalities AS '
                f'SELECT numeric_code, name, italian_name, other_language_name, province_abbreviation, cadastral_code '
                f'FROM {source}.municipalities'
            )
        )
        conn.execute(text(f'ALTER TABLE "{istat_schema}".municipalities ADD PRIMARY KEY (numeric_code)'))
    try:
        query = dict(url.query)
        query["options"] = f"-csearch_path={schema},{istat_schema}"
        schema_sync_url = sync_url.set(query=query)
        engine = create_engine(schema_sync_url)
        _apply_flat_schema(engine, schema, archive_schema, istat_schema)
        _apply_table_migrations(engine, schema, istat_schema)
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num varchar(64) NOT NULL PRIMARY KEY)"))
            conn.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": database.DATABASE_REVISION})
        engine.dispose()
        yield schema, url.set(query=query).render_as_string(hide_password=False), schema_sync_url
    finally:
        _drop(admin, schema)
        _drop(admin, archive_schema)
        _drop(admin, istat_schema)
        admin.dispose()


@pytest.fixture
async def fresh_db(pg_test_schema, monkeypatch):
    """Point ``sister.database`` at the throwaway schema with every table empty."""
    from sqlalchemy import create_engine, text

    import sister.database as database

    schema, async_dsn, sync_url = pg_test_schema
    archive_schema = schema + "_archive"
    assert _SCHEMA_RE.fullmatch(schema) and _SCHEMA_RE.fullmatch(archive_schema)
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            tables = conn.execute(
                text(
                    "SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN (:s, :a) "
                    "AND tablename <> 'alembic_version'"
                ),
                {"s": schema, "a": archive_schema},
            ).all()
            if tables:
                conn.execute(text("TRUNCATE " + ", ".join(f'"{sch}"."{t}"' for sch, t in tables) + " RESTART IDENTITY CASCADE"))
    finally:
        engine.dispose()

    monkeypatch.setattr(database, "DATABASE_DSN", async_dsn)
    monkeypatch.setattr(database, "DATABASE_SCHEMA", schema)
    monkeypatch.setattr(database, "DATABASE_ARCHIVE_SCHEMA", archive_schema)
    monkeypatch.setattr(database, "DATABASE_ISTAT_SCHEMA", schema + "_istat")
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_db_writable", None)
    yield
    if database._engine is not None:
        await database._engine.dispose()
        database._engine = None
