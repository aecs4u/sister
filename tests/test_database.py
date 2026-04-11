"""Tests for the SQLite database layer (database.py).

Uses a temporary SQLite file for isolation (in-memory won't work because
init_db() calls os.makedirs on the parent directory).
"""

import os
import tempfile

import pytest

import sister.database as database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _fresh_db(tmp_path, monkeypatch):
    """Point the database module at a fresh temp file for each test."""
    db_path = str(tmp_path / "test_visura.sqlite")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()
    yield


# ---------------------------------------------------------------------------
# init / schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_db_creates_tables():
    db = await database.get_db()
    try:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]
    finally:
        await db.close()

    assert "visura_requests" in tables
    assert "visura_responses" in tables


# ---------------------------------------------------------------------------
# save_request / save_requests_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_request_persists_row():
    await database.save_request(
        request_id="req_T_abc",
        request_type="visura",
        tipo_catasto="T",
        provincia="Trieste",
        comune="TRIESTE",
        foglio="9",
        particella="166",
    )

    db = await database.get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM visura_requests WHERE request_id = ?", ("req_T_abc",)
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    assert row is not None
    assert row["provincia"] == "Trieste"
    assert row["tipo_catasto"] == "T"


@pytest.mark.asyncio
async def test_save_request_with_optional_fields():
    await database.save_request(
        request_id="req_F_opt",
        request_type="intestati",
        tipo_catasto="F",
        provincia="Roma",
        comune="ROMA",
        foglio="100",
        particella="50",
        sezione="A",
        subalterno="3",
    )

    db = await database.get_db()
    try:
        cursor = await db.execute(
            "SELECT sezione, subalterno FROM visura_requests WHERE request_id = ?",
            ("req_F_opt",),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    assert row["sezione"] == "A"
    assert row["subalterno"] == "3"


@pytest.mark.asyncio
async def test_save_requests_batch_persists_all():
    rows = [
        {
            "request_id": f"req_batch_{i}",
            "request_type": "visura",
            "tipo_catasto": "T",
            "provincia": "Trieste",
            "comune": "TRIESTE",
            "foglio": "9",
            "particella": str(i),
        }
        for i in range(5)
    ]
    await database.save_requests_batch(rows)

    db = await database.get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM visura_requests")
        count = (await cursor.fetchone())[0]
    finally:
        await db.close()

    assert count == 5


@pytest.mark.asyncio
async def test_save_requests_batch_empty_is_noop():
    await database.save_requests_batch([])

    db = await database.get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM visura_requests")
        count = (await cursor.fetchone())[0]
    finally:
        await db.close()

    assert count == 0


# ---------------------------------------------------------------------------
# save_response / get_response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_get_response():
    await database.save_request(
        request_id="req_F_resp",
        request_type="visura",
        tipo_catasto="F",
        provincia="Trieste",
        comune="TRIESTE",
        foglio="9",
        particella="166",
    )

    await database.save_response(
        request_id="req_F_resp",
        success=True,
        tipo_catasto="F",
        data={"immobili": [{"Foglio": "9", "Particella": "166"}]},
    )

    result = await database.get_response("req_F_resp")

    assert result is not None
    assert result["success"] is True
    assert result["tipo_catasto"] == "F"
    assert result["data"]["immobili"][0]["Foglio"] == "9"
    assert result["error"] is None


@pytest.mark.asyncio
async def test_save_response_with_error():
    await database.save_request(
        request_id="req_F_err",
        request_type="visura",
        tipo_catasto="F",
        provincia="Roma",
        comune="ROMA",
        foglio="1",
        particella="1",
    )

    await database.save_response(
        request_id="req_F_err",
        success=False,
        tipo_catasto="F",
        error="Session expired",
    )

    result = await database.get_response("req_F_err")

    assert result is not None
    assert result["success"] is False
    assert result["error"] == "Session expired"
    assert result["data"] is None


@pytest.mark.asyncio
async def test_get_response_returns_none_for_missing():
    result = await database.get_response("nonexistent_id")
    assert result is None


@pytest.mark.asyncio
async def test_save_response_upserts_on_duplicate():
    await database.save_request(
        request_id="req_F_upsert",
        request_type="visura",
        tipo_catasto="F",
        provincia="Trieste",
        comune="TRIESTE",
        foglio="9",
        particella="166",
    )

    await database.save_response(
        request_id="req_F_upsert", success=False, tipo_catasto="F", error="first"
    )
    await database.save_response(
        request_id="req_F_upsert",
        success=True,
        tipo_catasto="F",
        data={"ok": True},
    )

    result = await database.get_response("req_F_upsert")
    assert result["success"] is True
    assert result["data"] == {"ok": True}


# ---------------------------------------------------------------------------
# find_responses
# ---------------------------------------------------------------------------


async def _seed_search_data():
    """Insert a handful of requests + responses for search tests."""
    entries = [
        ("r1", "visura", "T", "Trieste", "TRIESTE", "9", "100"),
        ("r2", "visura", "F", "Trieste", "TRIESTE", "9", "166"),
        ("r3", "visura", "T", "Roma", "ROMA", "50", "10"),
    ]
    for rid, rtype, tc, prov, com, fog, par in entries:
        await database.save_request(
            request_id=rid,
            request_type=rtype,
            tipo_catasto=tc,
            provincia=prov,
            comune=com,
            foglio=fog,
            particella=par,
        )
        await database.save_response(
            request_id=rid, success=True, tipo_catasto=tc, data={"id": rid}
        )


@pytest.mark.asyncio
async def test_find_responses_no_filters():
    await _seed_search_data()
    results = await database.find_responses()
    assert len(results) == 3


@pytest.mark.asyncio
async def test_find_responses_filter_by_provincia():
    await _seed_search_data()
    results = await database.find_responses(provincia="Trieste")
    assert len(results) == 2
    assert all(r["provincia"] == "Trieste" for r in results)


@pytest.mark.asyncio
async def test_find_responses_filter_by_tipo_catasto():
    await _seed_search_data()
    results = await database.find_responses(tipo_catasto="T")
    assert len(results) == 2


@pytest.mark.asyncio
async def test_find_responses_combined_filters():
    await _seed_search_data()
    results = await database.find_responses(provincia="Trieste", tipo_catasto="F")
    assert len(results) == 1
    assert results[0]["request_id"] == "r2"


@pytest.mark.asyncio
async def test_find_responses_limit_and_offset():
    await _seed_search_data()
    results = await database.find_responses(limit=1, offset=0)
    assert len(results) == 1

    results_page2 = await database.find_responses(limit=1, offset=1)
    assert len(results_page2) == 1
    assert results_page2[0]["request_id"] != results[0]["request_id"]


@pytest.mark.asyncio
async def test_find_responses_includes_response_data():
    await _seed_search_data()
    results = await database.find_responses(provincia="Roma")
    assert len(results) == 1
    assert results[0]["data"] == {"id": "r3"}
    assert results[0]["success"] is True


# ---------------------------------------------------------------------------
# cleanup_old_responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_old_responses_removes_expired():
    await database.save_request(
        request_id="req_old",
        request_type="visura",
        tipo_catasto="T",
        provincia="Trieste",
        comune="TRIESTE",
        foglio="1",
        particella="1",
    )
    await database.save_response(
        request_id="req_old", success=True, tipo_catasto="T", data={}
    )

    # Use negative TTL so everything is considered "old"
    deleted = await database.cleanup_old_responses(ttl_seconds=-1)
    assert deleted >= 1

    result = await database.get_response("req_old")
    assert result is None


@pytest.mark.asyncio
async def test_cleanup_old_responses_preserves_recent():
    await database.save_request(
        request_id="req_fresh",
        request_type="visura",
        tipo_catasto="T",
        provincia="Trieste",
        comune="TRIESTE",
        foglio="1",
        particella="1",
    )
    await database.save_response(
        request_id="req_fresh", success=True, tipo_catasto="T", data={}
    )

    # High TTL — nothing should be deleted
    deleted = await database.cleanup_old_responses(ttl_seconds=999999)
    assert deleted == 0

    result = await database.get_response("req_fresh")
    assert result is not None


# ---------------------------------------------------------------------------
# count_responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_responses_empty():
    stats = await database.count_responses()
    assert stats["total_requests"] == 0
    assert stats["total_responses"] == 0
    assert stats["successful"] == 0
    assert stats["failed"] == 0


@pytest.mark.asyncio
async def test_count_responses_with_data():
    await _seed_search_data()
    stats = await database.count_responses()
    assert stats["total_requests"] == 3
    assert stats["total_responses"] == 3
    assert stats["successful"] == 3
    assert stats["failed"] == 0
