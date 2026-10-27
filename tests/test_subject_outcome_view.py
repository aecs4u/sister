"""``v_subject_search_outcome``: the code asked comes from the job payload when the answer carried no subject."""

import importlib.util
import json
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

from sister import database

MIGRATION = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20261105_subject_search_outcome_v2.py"


def _create_view(sync_url, schema):
    spec = importlib.util.spec_from_file_location("_subject_outcome_v2", MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn, opts={"sister_schema": schema})):
                migration.upgrade()
    finally:
        engine.dispose()


async def _ask(request_id, code, *, tipo="T", success=None, error=None, subject_query=None):
    await database.save_request(
        request_id=request_id, request_type="soggetto_immobili", tipo_catasto=tipo, provincia="NAZIONALE",
        comune="", foglio="", particella=code, cache_key=None,
    )
    payload = {"request_id": request_id, "cadastre_type": tipo, "params": {"codice_fiscale": code, "azienda": "true"}}
    async with database._get_session_factory()() as session:
        await session.execute(
            text("INSERT INTO sister_request_jobs (request_id, request_type, payload_json) VALUES (:r, 'soggetto_immobili', :p)"),
            {"r": request_id, "p": json.dumps(payload)},
        )
        await session.commit()
    if success is not None or error is not None:
        data = {"soggetto": subject_query} if subject_query else None
        await database.save_response(request_id, bool(success), tipo, data=data, error=error)


async def _outcomes(sync_url, schema):
    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(f'SELECT fiscal_code, outcome, attempts, searched_types FROM "{schema}".v_subject_search_outcome ORDER BY 1')
            ).all()
    finally:
        engine.dispose()
    return {code: (outcome, attempts, types) for code, outcome, attempts, types in rows}


@pytest.mark.usefixtures("fresh_db")
async def test_outcome_view_uses_job_payload_and_new_outcomes(pg_test_schema):
    schema, _, sync_url = pg_test_schema
    _create_view(sync_url, schema)

    too_many = "Pagina inattesa dopo la ricerca (https://x/RicercaVisPNF.do): Superato il limite massimo di 400 elementi."
    await _ask("a1", "00000000018", error=too_many)  # no subject in the answer: the code comes from the job
    await _ask("a2", "00000000018", error=too_many)
    await _ask("b1", "01234567890", success=True, subject_query="01234567890", tipo="E")
    await _ask("c1", "09876543210", error="soggetto_E_x: NESSUNA CORRISPONDENZA TROVATA", subject_query="09876543210")
    await _ask("d1", "11111111113")  # queued, never answered
    await _ask("e1", "22222222226", error="Locator.click: Timeout 15000ms exceeded.")

    outcomes = await _outcomes(sync_url, schema)

    assert outcomes["00000000018"] == ("too_many", 2, "T")
    assert outcomes["01234567890"] == ("found", 1, "E")
    assert outcomes["09876543210"][0] == "no_match"
    assert outcomes["11111111113"][0] == "pending"
    assert outcomes["22222222226"][0] == "error"
