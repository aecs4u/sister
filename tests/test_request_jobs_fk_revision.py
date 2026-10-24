"""``20261030_request_jobs_fk`` ties ``sister_request_jobs`` to ``request`` and ``request_type``."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISION = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20261030_request_jobs_fk.py"


def _run(engine, step: str) -> None:
    spec = importlib.util.spec_from_file_location("request_jobs_fk_revision", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            getattr(module, step)()


def _fk_names(engine) -> set[str]:
    return {fk["name"] for fk in inspect(engine).get_foreign_keys("sister_request_jobs")}


def test_links_jobs_to_requests_and_is_repeatable(pg_test_schema):
    _schema, _async_dsn, sync_url = pg_test_schema
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            for name in ("sister_request_jobs_request_id_fkey", "sister_request_jobs_request_type_fkey"):
                conn.execute(text(f"ALTER TABLE sister_request_jobs DROP CONSTRAINT IF EXISTS {name}"))
            conn.execute(text("DELETE FROM sister_request_jobs"))
            conn.execute(text("INSERT INTO request_type (code) VALUES ('visura') ON CONFLICT DO NOTHING"))
            conn.execute(text("INSERT INTO request (request_ref, request_type, created_at) VALUES ('r1', 'visura', now())"))
            for ref in ("r1", "orphan"):
                conn.execute(text("INSERT INTO sister_request_jobs (request_id, request_type, payload_json) VALUES (:r, 'visura', '{}')"), {"r": ref})
        _run(engine, "upgrade")
        assert _fk_names(engine) == {"sister_request_jobs_request_id_fkey", "sister_request_jobs_request_type_fkey"}
        with engine.connect() as conn:
            assert conn.execute(text("SELECT request_id FROM sister_request_jobs")).scalars().all() == ["r1"]  # orphan removed
        _run(engine, "upgrade")  # second run changes nothing

        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO sister_request_jobs (request_id, request_type, payload_json) VALUES ('nope', 'visura', '{}')"))
        with engine.begin() as conn:  # deleting the request removes its job
            conn.execute(text("DELETE FROM request WHERE request_ref = 'r1'"))
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM sister_request_jobs")).scalar_one() == 0

        _run(engine, "downgrade")
        assert _fk_names(engine) == set()
    finally:
        engine.dispose()
