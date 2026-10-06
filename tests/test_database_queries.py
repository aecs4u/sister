"""Tests for database.py cache lookup, JSON→table projection, and web result queries.

Complements test_database.py (basic save/get/find/cleanup) using the shared
``fresh_db`` fixture and the realistic payloads in tests/fixtures.py.
"""

import copy
import gc
import warnings
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from sister import database
from sister.db_models import VisuraResponse as VisuraResponseRow
from tests.fixtures import (
    SISTER_INTESTATI_RESPONSE,
    SISTER_SEARCH_RESPONSE_FABBRICATI,
    SISTER_SEARCH_RESPONSE_TERRENI,
    SISTER_SOGGETTO_RESPONSE,
)


async def _request(request_id, *, tipo="F", provincia="Trieste", comune="TRIESTE", foglio="9", particella="166",
                   cache_key=None, request_type="visura"):
    await database.save_request(
        request_id=request_id,
        request_type=request_type,
        tipo_catasto=tipo,
        provincia=provincia,
        comune=comune,
        foglio=foglio,
        particella=particella,
        cache_key=cache_key,
    )


async def _age_response(request_id: str, seconds: int) -> None:
    """Backdate a stored response."""
    async with database._get_session_factory()() as session:
        await session.execute(
            update(VisuraResponseRow)
            .where(VisuraResponseRow.request_id == request_id)
            .values(created_at=datetime.now(timezone.utc) - timedelta(seconds=seconds))
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Cache key and cache lookup
# ---------------------------------------------------------------------------


def test_cache_key_is_order_independent_and_ignores_none():
    a = database.compute_cache_key("visura", province="Roma", sheet="1", section=None)
    b = database.compute_cache_key("visura", sheet="1", province="Roma")

    assert a == b
    assert len(a) == 64


def test_cache_key_differs_by_request_type_and_value():
    base = database.compute_cache_key("visura", province="Roma")

    assert database.compute_cache_key("intestati", province="Roma") != base
    assert database.compute_cache_key("visura", province="Milano") != base


@pytest.mark.usefixtures("fresh_db")
async def test_find_cached_response_returns_fresh_success():
    await _request("r1", cache_key="k")
    await database.save_response("r1", True, "F", data={"immobili": []})

    record = await database.find_cached_response("k", ttl_seconds=3600)

    assert record["request_id"] == "r1"
    assert record["success"] is True


@pytest.mark.usefixtures("fresh_db")
async def test_find_cached_response_ignores_failures_and_stale_rows():
    await _request("failed", cache_key="k")
    await database.save_response("failed", False, "F", error="boom")
    await _request("stale", cache_key="k")
    await database.save_response("stale", True, "F", data={})
    await _age_response("stale", 7200)

    assert await database.find_cached_response("k", ttl_seconds=3600) is None


@pytest.mark.usefixtures("fresh_db")
async def test_find_cached_response_prefers_newest():
    await _request("old", cache_key="k")
    await database.save_response("old", True, "F", data={"v": 1})
    await _age_response("old", 60)
    await _request("new", cache_key="k")
    await database.save_response("new", True, "F", data={"v": 2})

    assert (await database.find_cached_response("k", ttl_seconds=3600))["request_id"] == "new"


@pytest.mark.usefixtures("fresh_db")
async def test_cleanup_returns_count_and_leaves_fresh_rows():
    await _request("old")
    await database.save_response("old", True, "F", data={})
    await _age_response("old", 2 * 24 * 60 * 60)
    await _request("fresh")
    await database.save_response("fresh", True, "F", data={})

    deleted = await database.cleanup_old_responses(retention_days=1)

    assert deleted == 1
    assert await database.get_response("old") is None
    assert await database.get_response("fresh") is not None


# ---------------------------------------------------------------------------
# JSON → structured table projection
# ---------------------------------------------------------------------------


def test_parse_property_rows_splits_property_location_and_subject_fields():
    data = copy.deepcopy(SISTER_SEARCH_RESPONSE_FABBRICATI["data"])
    data["immobili"][0]["Codice fiscale"] = "RSSMRI85E28H501E"

    rows = database._parse_property_rows("r1", "F", data)

    assert len(rows) == 2
    prop, loc, subject = rows[0]
    assert prop["property_type"] == "building"
    assert prop["category"] == "A/2"
    assert prop["income"] == "500,00"
    assert (loc["sheet"], loc["parcel"], loc["subunit"]) == ("9", "166", "3")
    assert subject == {"fiscal_code": "RSSMRI85E28H501E"}


def test_parse_property_rows_maps_terreni_fields():
    ((prop, loc, _),) = database._parse_property_rows("r1", "T", SISTER_SEARCH_RESPONSE_TERRENI["data"])

    assert prop["property_type"] == "land"
    assert prop["quality"] == "SEMINATIVO"
    assert prop["dominical_income"] == "15,00"
    assert loc["cadastre_type"] == "T"


@pytest.mark.parametrize("data", [None, "not a dict", {"immobili": ["junk", 3]}])
def test_parse_property_rows_tolerates_bad_payloads(data):
    assert database._parse_property_rows("r1", "F", data) == []


def test_parse_owners_splits_subject_and_right():
    ((subject, right),) = database._parse_owners("r1", SISTER_INTESTATI_RESPONSE["data"])

    assert subject == {"display_name": "ROSSI MARIO", "fiscal_code": "RSSMRI85E28H501E"}
    assert right == {"right_type": "Proprietà per 1/1"}


def test_parse_owners_blank_values_become_none():
    ((subject, _),) = database._parse_owners("r1", {"intestati": [{"Nominativo": "  ", "Quota": "1/2"}]})

    assert subject == {"display_name": None}


def test_parse_page_visits_handles_bad_timestamps_and_serialises_extras():
    visits = database._parse_page_visits(
        "r1",
        {
            "page_visits": [
                {"step": "login", "timestamp": "2026-09-01T10:00:00", "errors": ["x"]},
                {"step": "search", "timestamp": "not-a-date", "form_elements": [{"name": "foglio"}]},
                "junk",
            ]
        },
    )

    assert [v.step for v in visits] == ["login", "search"]
    assert visits[0].timestamp == datetime(2026, 9, 1, 10, 0).astimezone()
    assert visits[0].errors_json == '["x"]'
    assert visits[1].timestamp is None
    assert "foglio" in visits[1].form_elements_json


def test_parse_page_visits_ignores_non_list():
    assert database._parse_page_visits("r1", {"page_visits": "oops"}) == []


def test_response_summary_keeps_only_int_counts_and_subject():
    summary = database._response_summary(
        {"total_results": 3, "total_intestati": True, "skipped_soppresso": "2", "soggetto": "RSSMRI85E28H501E"}
    )

    assert summary == {"total_results": 3, "subject_query": "RSSMRI85E28H501E"}
    assert database._response_summary(None) == {}


def test_parse_response_results_uses_position_when_index_invalid():
    rows = database._parse_response_results(
        {"results": [{"result_index": "7", "visura": {"a": 1}}, {"result_index": "x"}, "junk"]}
    )

    assert rows == [
        {"result_index": 7, "visura_present": True},
        {"result_index": 2, "visura_present": False},
    ]


@pytest.mark.parametrize("success,status", [(True, "completed"), (False, "failed"), (None, "pending")])
def test_single_result_status(success, status):
    assert database._single_result_status(success) == status


@pytest.mark.usefixtures("fresh_db")
async def test_save_response_projects_properties_with_request_location():
    await _request("r1", provincia="Trieste", comune="TRIESTE")
    await database.save_response("r1", True, "F", data=SISTER_SEARCH_RESPONSE_FABBRICATI["data"])

    props = await database.get_db_properties_for_response("r1")

    assert {p["subunit"] for p in props} == {"3", "5"}
    assert {p["province"] for p in props} == {"Trieste"}
    assert {p["municipality"] for p in props} == {"TRIESTE"}


@pytest.mark.usefixtures("fresh_db")
async def test_save_response_projects_owners():
    await _request("i1", request_type="intestati")
    await database.save_response("i1", True, "F", data=SISTER_INTESTATI_RESPONSE["data"])

    (owner,) = await database.get_db_owners_for_response("i1")

    assert owner["fiscal_code"] == "RSSMRI85E28H501E"
    assert owner["right_type"] == "Proprietà per 1/1"


@pytest.mark.usefixtures("fresh_db")
async def test_resaving_a_response_replaces_projected_rows():
    await _request("r1")
    await database.save_response("r1", True, "F", data=SISTER_SEARCH_RESPONSE_FABBRICATI["data"])
    await database.save_response("r1", True, "F", data={"immobili": [{"Foglio": "9", "Particella": "1"}]})

    assert len(await database.get_db_properties_for_response("r1")) == 1


@pytest.mark.usefixtures("fresh_db")
async def test_save_response_stores_subject_query_summary():
    await _request("s1", tipo="E", request_type="soggetto")
    await database.save_response("s1", True, "E", data=SISTER_SOGGETTO_RESPONSE["data"])

    rows = await database.find_result_rows()

    assert rows[0]["total_results"] == 1


# ---------------------------------------------------------------------------
# Web result listing and counts
# ---------------------------------------------------------------------------


@pytest.fixture()
async def populated(fresh_db):
    await _request("ok_F", tipo="F", provincia="Trieste", foglio="9")
    await database.save_response("ok_F", True, "F", data=SISTER_SEARCH_RESPONSE_FABBRICATI["data"])
    await _request("fail_T", tipo="T", provincia="Trieste", foglio="10")
    await database.save_response("fail_T", False, "T", error="NESSUNA CORRISPONDENZA TROVATA")
    await _request("pending_F", tipo="F", provincia="Roma", comune="ROMA", foglio="1")
    return fresh_db


@pytest.mark.usefixtures("populated")
@pytest.mark.parametrize(
    "filters,expected",
    [
        ({}, {"ok_F", "fail_T", "pending_F"}),
        ({"provincia": "Trieste"}, {"ok_F", "fail_T"}),
        ({"tipo_catasto": "T"}, {"fail_T"}),
        ({"foglio": 9}, {"ok_F"}),
        ({"status": "completed"}, {"ok_F"}),
        ({"status": "no_match"}, {"fail_T"}),
        ({"status": "pending"}, {"pending_F"}),
        ({"status": "bogus"}, {"ok_F", "fail_T", "pending_F"}),
        ({"source": "workflow"}, set()),
    ],
)
async def test_find_result_rows_filters(filters, expected):
    rows = await database.find_result_rows(**filters)

    assert {r["request_id"] for r in rows} == expected
    assert await database.count_total_result_rows(**filters) == len(expected)


@pytest.mark.usefixtures("populated")
async def test_no_match_has_a_separate_result_status_and_count():
    (row,) = [r for r in await database.find_result_rows(status="no_match") if r["request_id"] == "fail_T"]
    assert row["status"] == "no_match"
    assert row["success"] is False
    assert (await database.count_result_rows())["no_match"] == 1


@pytest.mark.usefixtures("populated")
async def test_find_result_rows_includes_projection_counts_and_status():
    rows = {r["request_id"]: r for r in await database.find_result_rows()}

    assert rows["ok_F"]["property_count"] == 2
    assert rows["ok_F"]["status"] == "completed"
    assert rows["fail_T"]["status"] == "no_match"
    assert rows["pending_F"]["status"] == "pending"
    assert rows["pending_F"]["success"] is None


@pytest.mark.usefixtures("populated")
async def test_find_result_rows_paginates():
    first = await database.find_result_rows(limit=2)
    rest = await database.find_result_rows(limit=2, offset=2)

    assert len(first) == 2
    assert len(rest) == 1
    assert {r["request_id"] for r in first + rest} == {"ok_F", "fail_T", "pending_F"}


@pytest.mark.usefixtures("populated")
async def test_count_result_rows_breakdown():
    stats = await database.count_result_rows(provincia="Trieste")

    assert stats["total_requests"] == 2
    assert stats["successful"] == 1
    assert stats["no_match"] == 1
    assert stats["failed"] == 0
    assert stats["pending"] == 0


async def test_result_queries_without_database_file(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "missing.sqlite"))

    assert await database.find_result_rows() == []
    assert await database.count_total_result_rows() == 0
    assert (await database.count_result_rows())["total_requests"] == 0


@pytest.mark.usefixtures("populated")
async def test_result_queries_close_their_sqlite_connections():
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        await database.find_result_rows()
        await database.count_total_result_rows()
        await database.count_result_rows()
        gc.collect()

    leaks = [w for w in caught if issubclass(w.category, ResourceWarning) and "sqlite3" in str(w.message)]
    assert leaks == []
