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

    from sqlalchemy import text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

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
        with Operations.context(MigrationContext.configure(conn)):
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
        conn.execute(
            text(
                """CREATE TABLE cadastral_map_geometry_cache (
                    feature_type TEXT NOT NULL,
                    lookup_key TEXT NOT NULL,
                    municipality TEXT NOT NULL DEFAULT '',
                    province TEXT NOT NULL DEFAULT '',
                    municipality_code TEXT NOT NULL DEFAULT '',
                    section TEXT NOT NULL DEFAULT '',
                    sheet TEXT NOT NULL DEFAULT '',
                    parcel TEXT NOT NULL DEFAULT '',
                    source_reference TEXT,
                    geom TEXT,
                    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (feature_type, lookup_key)
                )"""
            )
        )
        # DB-layer tests do not need PostGIS; provide the map view's schema
        # sentinel so init_db can validate the rest of the isolated schema.
        conn.execute(
            text(
                """CREATE VIEW v_sister_visura_cadastral_polygons AS
                   SELECT NULL::BIGINT AS document_id,
                          NULL::TEXT AS feature_type,
                          NULL::TEXT AS lookup_key,
                          NULL::BIGINT AS location_id,
                          NULL::TEXT AS municipality_code,
                          NULL::TEXT AS municipality,
                          NULL::TEXT AS province,
                          NULL::TEXT AS section,
                          NULL::TEXT AS sheet,
                          NULL::TEXT AS parcel,
                          NULL::TEXT AS source_reference,
                          NULL::TIMESTAMPTZ AS checked_at,
                          NULL::TEXT AS geom
                   WHERE FALSE"""
            )
        )


def _apply_enum_catalog_triggers(engine) -> None:
    """Mirror the catalog triggers added by Alembic in this ORM-created test schema.

    The isolated test schema is built with ``SQLModel.metadata.create_all`` rather
    than by replaying the full production migration chain. The model includes the
    catalog foreign keys, so these triggers are needed to keep newly observed
    categorical values valid just as they are in a migrated production schema.
    """
    from sqlalchemy import text

    from sister.db_models import ENUM_CATALOG_TABLES
    from sqlmodel import SQLModel

    groups = {}
    for table in SQLModel.metadata.tables.values():
        fields = []
        for column in table.columns:
            for foreign_key in column.foreign_keys:
                target = foreign_key.target_fullname
                catalog_name = target.split(".", 1)[0]
                if catalog_name in ENUM_CATALOG_TABLES:
                    fields.append((column.name, catalog_name))
        if fields:
            groups[table.name] = fields

    with engine.begin() as conn:
        conn.execute(
            text(
                """CREATE FUNCTION ensure_enum_reference_value()
                   RETURNS trigger
                   LANGUAGE plpgsql
                   AS $$
                   DECLARE
                       pair_index INTEGER;
                       source_column TEXT;
                       enum_table TEXT;
                       source_value TEXT;
                   BEGIN
                       FOR pair_index IN 0..(TG_NARGS / 2 - 1) LOOP
                           source_column := TG_ARGV[pair_index * 2];
                           enum_table := TG_ARGV[pair_index * 2 + 1];
                           source_value := to_jsonb(NEW) ->> source_column;
                           IF source_value IS NOT NULL THEN
                               EXECUTE format(
                                   'INSERT INTO %I (value) VALUES ($1) ON CONFLICT (value) DO NOTHING',
                                   enum_table
                               ) USING source_value;
                           END IF;
                       END LOOP;
                       RETURN NEW;
                   END;
                   $$"""
            )
        )
        for table_name, fields in groups.items():
            trigger_name = f"trg_enum_catalog_{table_name}"
            args = ", ".join(f"'{column}', '{catalog}'" for column, catalog in fields)
            conn.execute(
                text(
                    f"CREATE TRIGGER {trigger_name} BEFORE INSERT OR UPDATE ON {table_name} "
                    f"FOR EACH ROW EXECUTE FUNCTION ensure_enum_reference_value({args})"
                )
            )


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
        _apply_enum_catalog_triggers(engine)
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
    monkeypatch.setattr(database, "DATABASE_SCHEMA", schema)
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_db_writable", None)
    yield
    if database._engine is not None:
        await database._engine.dispose()
        database._engine = None
