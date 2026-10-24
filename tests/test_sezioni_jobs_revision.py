"""``20261029_sezioni_jobs_table`` creates the section-extraction job table where it is missing, and only there."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

REVISION = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20261029_sezioni_jobs_table.py"


def _revision():
    spec = importlib.util.spec_from_file_location("sezioni_jobs_table_revision", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade(engine) -> None:
    revision = _revision()
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            revision.upgrade()


def test_creates_the_missing_table_and_a_second_run_changes_nothing(pg_test_schema):
    _schema, _async_dsn, sync_url = pg_test_schema
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:  # the live state: the table is absent
            conn.execute(text("DROP TABLE IF EXISTS sezioni_extraction_jobs"))
        _upgrade(engine)
        inspector = inspect(engine)
        assert inspector.has_table("sezioni_extraction_jobs")
        columns = {c["name"] for c in inspector.get_columns("sezioni_extraction_jobs")}
        assert columns == {"job_id", "cadastre_type", "max_provinces", "status", "progress_json", "results_json", "error", "created_at", "updated_at"}
        assert "ix_sezioni_extraction_jobs_status" in {i["name"] for i in inspector.get_indexes("sezioni_extraction_jobs")}

        with engine.begin() as conn:
            conn.execute(text("INSERT INTO sezioni_extraction_jobs (job_id, cadastre_type, max_provinces, status, created_at, updated_at) "
                              "VALUES ('keep', 'T', 1, 'queued', 'x', 'x')"))
        _upgrade(engine)  # already there: left alone, rows kept
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM sezioni_extraction_jobs")).scalar_one() == 1
    finally:
        engine.dispose()


def test_database_expects_the_alembic_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from sister import database

    config = Config(str(REVISION.parent.parent.parent / "alembic.ini"))
    config.set_main_option("script_location", str(REVISION.parent.parent))
    assert ScriptDirectory.from_config(config).get_current_head() == database.DATABASE_REVISION
