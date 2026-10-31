"""Tests for the database layer (database.py).

Run against an isolated throwaway PostgreSQL schema (see ``tests/pg_isolation.py``); skipped without a local PostgreSQL.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event, text

import sister.database as database
from sister.db_models import (
    Document,
    DocumentContent,
    ManualPropertyValuation,
    PropertyAffrancationAmount,
    PropertyOmiOverride,
    PropertyOmiSelection,
    PropertyValuationPreference,
    Request,
    Subject,
)
from sister.visura_xml_models import DocumentSubject, MutationOwner, OwnershipMutation

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _fresh_db(fresh_db):
    """Every test starts on empty tables of the throwaway schema."""
    await database.init_db()


async def _rows(sql: str, **params) -> list[dict]:
    async with database._get_session_factory()() as session:
        result = await session.execute(text(sql), params)
        return [dict(row._mapping) for row in result]


# ---------------------------------------------------------------------------
# init / schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browse_metadata_select_does_not_load_archived_xml():
    async with database._get_session_factory()() as session:
        document = Document(file_name="metadata-only.xml", file_size=24)
        session.add(document)
        await session.flush()
        document_id = document.id
        session.add(DocumentContent(document_id=document_id, content="<Sensitive>archived XML</Sensitive>"))
        session.add_all(
            Document(file_name=f"metadata-only-extra-{index}.xml", file_size=24)
            for index in range(101)
        )
        await session.commit()

    sql = str(database._document_select(include_content=False)).lower()
    assert "document_content" not in sql
    assert "request" in sql

    browse_rows = await database.get_all_documents()
    unbounded_rows = await database.get_all_documents(limit=None)
    selected_rows = await database.get_documents_by_ids([document_id])
    selected_with_content = await database.get_documents_by_ids([document_id], include_content=True)
    detail = await database.get_document_by_id(document_id)

    assert len(browse_rows) == 100
    assert len(unbounded_rows) == 102
    unbounded_document = next(row for row in unbounded_rows if row["id"] == document_id)
    assert selected_rows[0]["id"] == document_id
    assert unbounded_document["xml_content"] == selected_rows[0]["xml_content"] == ""
    assert selected_with_content[0]["xml_content"] == "<Sensitive>archived XML</Sensitive>"
    assert detail["xml_content"] == "<Sensitive>archived XML</Sensitive>"


@pytest.mark.asyncio
async def test_get_user_manual_property_valuations_reads_all_sources_in_one_result():
    user_id = "valuation-reader@example.com"
    asset_key = "a" * 64
    async with database._get_session_factory()() as session:
        session.add_all([
            ManualPropertyValuation(
                user_id=user_id,
                asset_key=asset_key,
                provider="immobiliare",
                value_min=Decimal("180000.00"),
                value_max=Decimal("210000.00"),
                source_date=date(2026, 10, 1),
                source_url="https://example.test/immobiliare",
                notes="first source",
            ),
            ManualPropertyValuation(
                user_id=user_id,
                asset_key=asset_key,
                provider="idealista",
                value_min=Decimal("175000.00"),
                value_max=None,
            ),
            PropertyValuationPreference(
                user_id=user_id,
                asset_key=asset_key,
                preferred_source="idealista",
            ),
            PropertyAffrancationAmount(
                user_id=user_id,
                asset_key=asset_key,
                amount=Decimal("12500.00"),
            ),
            PropertyOmiOverride(
                user_id=user_id,
                asset_key=asset_key,
                value_min=Decimal("1900.00"),
                value_max=Decimal("2200.00"),
            ),
            PropertyOmiSelection(
                user_id=user_id,
                asset_key=asset_key,
                zone_code="B12",
                typology_code=20,
                is_manual=True,
            ),
        ])
        await session.commit()

    statements = []

    def _record_query(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    engine = database._get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", _record_query)
    try:
        values = await database.get_user_manual_property_valuations(user_id, [asset_key, "invalid"])
    finally:
        event.remove(engine, "before_cursor_execute", _record_query)

    assert len(statements) == 1
    assert "UNION ALL" in statements[0].upper()
    assert values == {
        asset_key: {
            "valuations": [
                {
                    "provider": "idealista",
                    "value_min": Decimal("175000.00"),
                    "value_max": None,
                    "source_date": None,
                    "source_url": None,
                    "notes": None,
                },
                {
                    "provider": "immobiliare",
                    "value_min": Decimal("180000.00"),
                    "value_max": Decimal("210000.00"),
                    "source_date": date(2026, 10, 1),
                    "source_url": "https://example.test/immobiliare",
                    "notes": "first source",
                },
            ],
            "preferred_source": "idealista",
            "affrancation_amount": Decimal("12500.00"),
            "omi_override": {"value_min": Decimal("1900.00"), "value_max": Decimal("2200.00")},
            "omi_selection": {"zone_code": "B12", "typology_code": 20, "is_manual": True},
        }
    }


@pytest.mark.asyncio
async def test_streamed_document_metadata_uses_bounded_cursor_and_omits_archived_xml():
    async with database._get_session_factory()() as session:
        document = Document(file_name="streamed-metadata.xml", file_size=24)
        session.add(document)
        await session.flush()
        document_id = document.id
        session.add(DocumentContent(document_id=document_id, content="<Private>archive</Private>"))
        await session.commit()

    streamed_rows = [row async for row in database.iter_all_documents(batch_size=1)]

    assert len(streamed_rows) == 1
    assert streamed_rows[0]["id"] == document_id
    assert streamed_rows[0]["xml_content"] == ""


@pytest.mark.asyncio
async def test_document_group_siblings_loads_request_and_basename_matches_without_content():
    async with database._get_session_factory()() as session:
        request = Request(request_ref="subject-sibling-request")
        other_request = Request(request_ref="other-sibling-request")
        session.add_all([request, other_request])
        await session.flush()
        primary = Document(
            request_id=request.id,
            file_name="visura.p7m",
            file_format="P7M",
        )
        same_request = Document(
            request_id=request.id,
            file_name="other-document.pdf",
            file_format="PDF",
        )
        same_stem = Document(
            request_id=other_request.id,
            file_name="visura.pdf",
            file_format="PDF",
        )
        session.add_all([primary, same_request, same_stem])
        await session.flush()
        primary_id = primary.id
        same_request_id = same_request.id
        same_stem_id = same_stem.id
        session.add(DocumentContent(document_id=primary_id, content="<Sensitive>archived XML</Sensitive>"))
        await session.commit()

    siblings = await database.get_document_group_siblings({primary_id})

    assert {item["id"] for item in siblings} == {primary_id, same_request_id, same_stem_id}
    assert all(item["xml_content"] == "" for item in siblings)


@pytest.mark.asyncio
async def test_document_owner_fiscal_codes_uses_normalized_mutation_owners():
    owner_code = "RSSMRA85T10A562S"
    async with database._get_session_factory()() as session:
        property_document = Document(file_name="owner-linked-property.xml")
        unrelated_document = Document(file_name="owner-unlinked-property.xml")
        other_property_document = Document(file_name="owner-linked-other-property.xml")
        owner = Subject(name="Mario Rossi", fiscal_code=owner_code)
        subject_without_code = Subject(name="Unknown owner")
        session.add_all([property_document, unrelated_document, other_property_document, owner, subject_without_code])
        await session.flush()
        mutation = OwnershipMutation(document_id=property_document.id, is_current=True)
        other_mutation = OwnershipMutation(document_id=other_property_document.id, is_current=True)
        session.add_all([mutation, other_mutation])
        await session.flush()
        session.add_all(
            [
                MutationOwner(mutation_id=mutation.id, subject_id=owner.id),
                MutationOwner(mutation_id=mutation.id, subject_id=subject_without_code.id),
                MutationOwner(mutation_id=other_mutation.id, subject_id=owner.id),
            ]
        )
        property_document_id = property_document.id
        unrelated_document_id = unrelated_document.id
        other_property_document_id = other_property_document.id
        await session.commit()

    owners = await database.get_document_owner_fiscal_codes(
        [property_document_id, unrelated_document_id, property_document_id]
    )

    assert owners == {property_document_id: [owner_code]}
    assert await database.get_document_owner_fiscal_codes([]) == {}
    assert await database.get_document_owner_fiscal_codes([], fiscal_codes=[owner_code.lower()]) == {
        property_document_id: [owner_code],
        other_property_document_id: [owner_code],
    }


@pytest.mark.asyncio
async def test_subject_document_ids_for_fiscal_codes_batches_normalized_codes():
    owner_code = "RSSMRA85T10A562S"
    other_code = "VRNGPP80A01F205X"
    async with database._get_session_factory()() as session:
        owner_document = Document(file_name="owner-subject.pdf")
        other_document = Document(file_name="other-subject.pdf")
        unrelated_document = Document(file_name="unrelated-subject.pdf")
        owner = Subject(name="Mario Rossi", fiscal_code=owner_code)
        other = Subject(name="Giuseppe Verdi", fiscal_code=other_code)
        unrelated = Subject(name="Unrelated Person", fiscal_code="BNCLGU75A01H501Z")
        session.add_all([owner_document, other_document, unrelated_document, owner, other, unrelated])
        await session.flush()
        session.add_all(
            [
                DocumentSubject(document_id=owner_document.id, subject_id=owner.id),
                DocumentSubject(document_id=other_document.id, subject_id=other.id),
                DocumentSubject(document_id=unrelated_document.id, subject_id=unrelated.id),
            ]
        )
        expected_ids = {owner_document.id, other_document.id}
        await session.commit()

    assert await database.get_subject_document_ids_for_fiscal_codes([owner_code.lower(), other_code]) == expected_ids
    assert await database.get_subject_document_ids_for_fiscal_codes([]) == set()


@pytest.mark.asyncio
async def test_init_db_creates_tables():
    tables = [r["tablename"] for r in await _rows("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")]

    for table in ("cadastral_unit", "comune_pre_istat", "subject", "request", "request_result", "property", "result_owner", "document"):
        assert table in tables
    assert "comune" not in tables
    shared = await _rows(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = :schema AND table_name = 'municipalities'",
        schema=database.DATABASE_ISTAT_SCHEMA,
    )
    assert shared == [{"table_name": "municipalities"}]
    # the old layout's request/response split and per-purpose location/place tables are gone
    for table in ("cadastral_locations", "visura_requests", "visura_responses", "geographic_places"):
        assert table not in tables


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

    rows = await _rows(
        "SELECT req.request_ref AS request_id, c.province_abbreviation AS provincia, u.cadastre_type AS tipo_catasto, upper(c.italian_name) AS comune"
        " FROM request req LEFT JOIN cadastral_unit u ON req.cadastral_unit_id = u.id"
        " LEFT JOIN municipalities c ON c.numeric_code = u.comune_id WHERE req.request_ref = :rid",
        rid="req_T_abc",
    )
    row = rows[0] if rows else None

    assert row is not None
    assert row["provincia"] == "TS"  # the portal form takes the province name, the database stores the sigla
    assert row["comune"] == "TRIESTE"
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

    rows = await _rows(
        "SELECT u.section AS sezione, u.subunit AS subalterno"
        " FROM request req LEFT JOIN cadastral_unit u ON req.cadastral_unit_id = u.id"
        " WHERE req.request_ref = :rid",
        rid="req_F_opt",
    )
    row = rows[0]

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

    count = (await _rows("SELECT COUNT(*) AS n FROM request"))[0]["n"]

    assert count == 5


@pytest.mark.asyncio
async def test_save_requests_batch_empty_is_noop():
    await database.save_requests_batch([])

    count = (await _rows("SELECT COUNT(*) AS n FROM request"))[0]["n"]

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
async def test_get_result_record_includes_request_metadata_and_status():
    await database.save_request(
        request_id="req_F_detail",
        request_type="visura",
        tipo_catasto="F",
        provincia="Trieste",
        comune="TRIESTE",
        foglio="9",
        particella="166",
        sezione="A",
        subalterno="3",
    )

    await database.save_response(
        request_id="req_F_detail",
        success=True,
        tipo_catasto="F",
        data={"immobili": [{"Foglio": "9"}], "page_visits": [{"step": "search", "url": "/foo"}]},
    )

    result = await database.get_result_record("req_F_detail")

    assert result is not None
    assert result["status"] == "completed"
    assert result["request_type"] == "visura"
    assert result["provincia"] == "Trieste"
    assert result["sezione"] == "A"
    assert result["subalterno"] == "3"
    assert result["page_visits"][0]["step"] == "search"


@pytest.mark.asyncio
async def test_get_result_record_returns_pending_for_request_without_response():
    await database.save_request(
        request_id="req_pending",
        request_type="visura",
        tipo_catasto="T",
        provincia="Roma",
        comune="ROMA",
        foglio="1",
        particella="2",
    )

    result = await database.get_result_record("req_pending")

    assert result is not None
    assert result["status"] == "pending"
    assert result["success"] is None
    assert result["data"] is None


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
async def test_get_result_record_returns_none_for_missing():
    result = await database.get_result_record("nonexistent_id")
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

    await database.save_response(request_id="req_F_upsert", success=False, tipo_catasto="F", error="first")
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
        await database.save_response(request_id=rid, success=True, tipo_catasto=tc, data={"id": rid})


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
    await database.save_response(request_id="req_old", success=True, tipo_catasto="T", data={})

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
    await database.save_response(request_id="req_fresh", success=True, tipo_catasto="T", data={})

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
    assert stats["pending"] == 0


@pytest.mark.asyncio
async def test_count_responses_with_data():
    await _seed_search_data()
    stats = await database.count_responses()
    assert stats["total_requests"] == 3
    assert stats["total_responses"] == 3
    assert stats["successful"] == 3
    assert stats["failed"] == 0
    assert stats["pending"] == 0


@pytest.mark.asyncio
async def test_count_responses_separates_no_match_from_failures():
    await database.save_request(
        request_id="req_no_match_stats",
        request_type="visura",
        tipo_catasto="T",
        provincia="Roma",
        comune="ROMA",
        foglio="1",
        particella="9",
    )
    await database.save_response(
        request_id="req_no_match_stats",
        success=False,
        tipo_catasto="T",
        error="nessuna corrispondenza trovata",
    )

    stats = await database.count_responses()

    assert stats["total_requests"] == stats["total_responses"] == 1
    assert stats["successful"] == stats["failed"] == stats["pending"] == 0
    assert stats["no_match"] == 1


@pytest.mark.asyncio
async def test_count_responses_includes_pending_requests():
    await database.save_request(
        request_id="req_pending_stats",
        request_type="visura",
        tipo_catasto="T",
        provincia="Roma",
        comune="ROMA",
        foglio="1",
        particella="9",
    )

    stats = await database.count_responses()
    assert stats["total_requests"] == 1
    assert stats["total_responses"] == 0
    assert stats["pending"] == 1
