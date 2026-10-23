"""OMI zone overview: quote history shaping, variations, successor hint and the cross-source comparison."""

import pytest

from sister import omi_zone_overview as overview
from sister.omi_zone_overview import (
    build_affordability,
    build_comparison,
    build_zone_overview,
    clean_description,
    find_successor,
    parse_number,
    rebase_series,
)


def row(anno, semestre, zona="D4", code=20, label="Abitazioni civili", state="NORMALE", prevalent="P", low=6000.0,
        high=9000.0, rent_low="15,0", rent_high="23,0", description="'MARINA PICCOLA, CASTELLO,'", comune="CAPRI"):
    return {
        "anno": anno, "semestre": semestre, "zona": zona, "fascia": zona[0], "zona_descrizione": description,
        "link_zona": "NA1", "comune": comune, "provincia": "NA", "regione": "CAMPANIA", "cod_comune_amm": "B696",
        "cod_comune_istat": 15063014.0, "cod_tipologia": code, "tipologia": label, "stato_conservazione": state,
        "stato_prevalente": prevalent, "prezzo_min": low, "prezzo_max": high, "superficie_nl_compravendita": "L",
        "locazione_min": rent_low, "locazione_max": rent_high, "superficie_nl_locazione": "L",
    }


@pytest.mark.parametrize("raw, expected", [
    ("15,2", 15.2), ("1.234,5", 1234.5), (7400.0, 7400.0), ("4", 4.0), ("", None), ("-", None), (None, None),
    ("n.d.", None), (True, None),
])
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


def test_description_loses_quotes_and_trailing_comma():
    assert clean_description("'MARINA PICCOLA,  CASTELLO,'") == "MARINA PICCOLA, CASTELLO"


def test_rebase_is_100_on_the_base_year_and_empty_without_it():
    assert rebase_series({2018: 50.0, 2019: 55.0}, 2018) == {2018: 100.0, 2019: pytest.approx(110.0)}
    assert rebase_series({2019: 55.0}, 2018) == {}


def test_unknown_comune_is_not_found():
    assert build_zone_overview([], "D4") == {"found": False}


def test_variations_follow_the_semesters_not_the_row_order():
    rows = [row(2014 + i // 2, i % 2 + 1, low=1000.0 + 100 * i, high=2000.0 + 100 * i) for i in range(12)]
    result = build_zone_overview(list(reversed(rows)), "d4")
    civili = result["typologies"][0]["sale"]
    last, previous = 1500.0 + 100 * 11, 1500.0 + 100 * 10
    assert civili["last"] == last and civili["periodo"] == "2019S2" and civili["first_periodo"] == "2014S1"
    assert civili["vs_semester"] == pytest.approx((last / previous - 1) * 100)
    assert civili["vs_year"] == pytest.approx((last / (1500.0 + 100 * 9) - 1) * 100)
    assert civili["vs_5_years"] == pytest.approx((last / (1500.0 + 100) - 1) * 100)  # 10 semesters back
    assert civili["since_start"] == pytest.approx((last / 1500.0 - 1) * 100)
    assert civili["peak_periodo"] == "2019S2" and civili["from_peak"] == 0


def test_duplicated_zone_rows_count_once():
    rows = [row(2024, 1), row(2024, 1)]
    assert len(build_zone_overview(rows, "D4")["latest_quotes"]) == 1


def test_trend_uses_the_state_prevalent_in_most_periods():
    rows = [
        row(2020, 1, state="NORMALE"), row(2020, 1, state="OTTIMO", prevalent="", low=9000.0, high=12000.0),
        row(2020, 2, state="NORMALE"), row(2020, 2, state="OTTIMO", prevalent="", low=9000.0, high=12000.0),
        row(2021, 1, state="NORMALE", prevalent=""), row(2021, 1, state="OTTIMO", prevalent="P", low=9500.0, high=12500.0),
    ]
    typology = build_zone_overview(rows, "D4")["typologies"][0]
    assert typology["state"] == "NORMALE"
    assert [point["mid"] for point in typology["series"]] == [7500.0, 7500.0, 7500.0]


def test_yield_is_yearly_rent_over_price():
    quote = build_zone_overview([row(2024, 1, low=6000.0, high=10000.0, rent_low="20,0", rent_high="30,0")], "D4")
    assert quote["latest_quotes"][0]["yield"] == pytest.approx(25.0 * 12 / 8000.0 * 100)


def test_zone_missing_from_newest_release_is_discontinued_and_gaps_are_listed():
    rows = [row(2023, 1), row(2024, 1), row(2025, 1, zona="D5", description="'MARINA PICCOLA, CASTELLO'")]
    result = build_zone_overview(rows, "D4")
    assert result["discontinued"] is True and result["last_period"] == "2024S1" and result["newest_period"] == "2025S1"
    assert result["gap_periods"] == ["2023S2"]
    assert result["zones_newest"] == ["D5"]


def test_successor_needs_a_similar_description_or_the_same_quote():
    rows = [
        row(2024, 1), row(2025, 1, zona="D5", description="'MARINA PICCOLA, CASTELLO'"),
        row(2025, 1, zona="B1", description="'CENTRO STORICO'", low=9000.0, high=14000.0),
    ]
    result = build_zone_overview(rows, "D4")
    assert result["successor"]["zona"] == "D5" and result["successor"]["same_quote"] is True
    unrelated = build_zone_overview([row(2024, 1), row(2025, 1, zona="B1", description="'CENTRO STORICO'")], "D4")
    assert unrelated["successor"] is None and find_successor(unrelated) is None


def test_active_zone_has_no_successor_and_is_ranked_against_the_comune():
    rows = [
        row(2025, 1, zona="D4", low=6000.0, high=8000.0), row(2025, 1, zona="B1", low=8000.0, high=12000.0),
        row(2025, 1, zona="D3", low=3000.0, high=5000.0),
    ]
    result = build_zone_overview(rows, "D4")
    assert result["discontinued"] is False and result["successor"] is None
    comparison = result["comparison"]
    assert comparison["rank"] == 2 and comparison["of"] == 3
    assert comparison["vs_comune"] == pytest.approx((7000.0 / ((10000.0 + 7000.0 + 4000.0) / 3) - 1) * 100)


def test_zone_list_without_a_zone():
    result = build_zone_overview([row(2025, 1, zona="B1"), row(2025, 1, zona="D3")], "")
    assert result["zone_found"] is False and [z["zona"] for z in result["zones"]] == ["B1", "D3"]


def test_unknown_zone_keeps_the_comune_zone_list():
    result = build_zone_overview([row(2025, 1, zona="B1")], "ZZ")
    assert result["found"] and result["zone_found"] is False and result["zones_newest"] == ["B1"]


def test_comparison_starts_every_series_on_the_same_year():
    annual = {2016: 90.0, 2017: 100.0, 2018: 110.0, 2019: 121.0}
    context = {
        "hpi": {"index": [{"year": y, "index": 100.0 + y - 2015} for y in range(2014, 2020)]},
        "income": [{"year": y, "average_income": 20000.0 + 1000 * (y - 2017)} for y in range(2017, 2019)],
        "population": [{"year": y, "population": 1000 - (y - 2017)} for y in range(2017, 2020)],
    }
    chart = build_comparison(annual, context)
    assert chart["base_year"] == 2017 and chart["years"] == [2017, 2018, 2019]
    assert all(values[0] == pytest.approx(100.0) for values in chart["series"].values())
    assert chart["series"]["income"][-1] is None  # income ends in 2018
    assert chart["series"]["zone"][-1] == pytest.approx(121.0)


def test_population_that_misses_the_base_year_is_left_out():
    annual = {2017: 100.0, 2018: 110.0}
    context = {
        "hpi": {"index": [{"year": 2017, "index": 100.0}, {"year": 2018, "index": 102.0}]},
        "population": [{"year": 2018, "population": 10}],
    }
    assert "population" not in build_comparison(annual, context)["series"]


def test_comparison_is_empty_without_context():
    assert build_comparison({2017: 100.0, 2018: 110.0}, None)["years"] == []


def test_affordability_is_years_of_income_for_the_reference_dwelling():
    income = [{"year": 2020, "average_income": 25000.0}, {"year": 2021, "average_income": 26000.0}]
    rows = build_affordability({2020: 5000.0}, income, area=70)
    assert rows == [{"year": 2020, "price": 5000.0, "income": 25000.0, "years_of_income": pytest.approx(14.0)}]


def test_istat_code_from_the_omi_regional_code():
    assert overview._istat_numeric_code(15063014.0) == "063014"
    assert overview._istat_numeric_code(None) is None


def test_ntn_skips_years_without_any_value():
    empty = dict.fromkeys((
        "ntn_totale", "ntn_fino_50mq", "ntn_50_85mq", "ntn_85_115mq", "ntn_115_145mq", "ntn_oltre_145mq",
        "ntn_uffici", "ntn_negozi_lab", "ntn_depositi_comm_autorimesse", "ntn_depositi_comm", "ntn_produttivo",
        "ntn_box", "ntn_depositi_pertinenziali",
    ))
    filled = empty | {"anno": 2023, "ntn_totale": "35,4", "ntn_negozi_lab": "4"}
    summary = overview._ntn_summary([empty | {"anno": 2022}, filled])
    assert [item["year"] for item in summary["series"]] == [2023]
    assert summary["series"][0]["residential"] == 35.4 and summary["series"][0]["other"]["negozi_laboratori"] == 4.0
    assert overview._ntn_summary([empty | {"anno": 2022}]) == {}


@pytest.mark.asyncio
async def test_loader_reports_an_unreachable_database_as_none(monkeypatch, tmp_path):
    monkeypatch.setattr(overview, "omi_dsn", lambda: "postgresql://nobody@127.0.0.1:1/omi")
    monkeypatch.setattr(overview, "_memory", {})
    monkeypatch.setattr(overview, "_retry_at", {})
    monkeypatch.setenv("SISTER_OMI_CACHE_DIR", str(tmp_path))
    assert await overview.load_zone_overview("B696", "D4") is None
    assert await overview.load_zone_overview("B696", "D4") is None  # backing off, no second connection attempt


def test_chart_data_is_json_serialisable():
    import json

    result = build_zone_overview([row(2024, 1), row(2024, 2), row(2024, 2, zona="B1")], "D4")
    result["context"] = None
    data = overview.overview_chart_data(result)
    assert json.dumps(data) and data["typologies"][0]["labels"] == ["2024S1", "2024S2"]


def _client():
    from fastapi.testclient import TestClient

    from sister.main import app

    return TestClient(app, raise_server_exceptions=False)


def _loaded(monkeypatch, rows, zona):
    seen = []
    async def fake_loader(comune, zone, refresh=False):
        seen.append(refresh)
        result = build_zone_overview(rows, zone)
        result["context"] = None
        if result.get("zone_found"):
            result["comparison_chart"] = {"years": []}
            result["affordability"] = []
        return result

    monkeypatch.setattr(overview, "load_zone_overview", fake_loader)
    return seen


def test_page_renders_the_zone_overview_with_charts_and_a_successor_hint(monkeypatch):
    rows = [row(2023, 1), row(2024, 1), row(2025, 1, zona="D5", description="'MARINA PICCOLA, CASTELLO'")]
    _loaded(monkeypatch, rows, "D4")
    response = _client().get("/omi/valuation?comune=B696&zona=D4")
    assert response.status_code == 200
    html = response.text
    assert "Zona non più pubblicata" in html and "Probabile zona successiva" in html and "zona=D5" in html
    assert 'id="omiOverviewData"' in html and "chart.umd.min.js" in html and "omi-zone-overview.js" in html
    assert "onclick=" not in html  # the CSP forbids inline handlers


def test_refresh_parameter_reaches_the_loader(monkeypatch):
    seen = _loaded(monkeypatch, [row(2025, 1)], "D4")
    _client().get("/omi/valuation?comune=B696&zona=D4")
    _client().get("/omi/valuation?comune=B696&zona=D4&refresh=true")
    assert seen == [False, True]


def test_page_lists_the_zones_of_a_comune_without_a_zone(monkeypatch):
    _loaded(monkeypatch, [row(2025, 1, zona="B1"), row(2025, 1, zona="D3")], "")
    html = _client().get("/omi/valuation?comune=B696").text
    assert "Zone OMI di CAPRI" in html and "zona=B1" in html and "zona=D3" in html and "omiPriceChart" not in html


def test_page_reports_an_unavailable_database(monkeypatch):
    async def unavailable(comune, zone, refresh=False):
        return None

    monkeypatch.setattr(overview, "load_zone_overview", unavailable)
    response = _client().get("/omi/valuation?comune=B696&zona=D4")
    assert response.status_code == 503 and "temporaneamente disponibili" in response.text


def test_page_rejects_a_zone_without_a_comune():
    assert _client().get("/omi/valuation?zona=D4").status_code == 422


@pytest.fixture
def cache_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("SISTER_OMI_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(overview, "_memory", {})
    monkeypatch.setattr(overview, "_retry_at", {})
    monkeypatch.setattr(overview, "omi_dsn", lambda: "postgresql://x@127.0.0.1:1/omi")
    return tmp_path


def _fake_fetch(monkeypatch, calls, rows=None, context=None):
    async def fake(code, zone):
        calls.append((code, zone))
        return {"format": overview.CACHE_FORMAT, "saved_at": __import__("time").time(),
                "rows": rows if rows is not None else [row(2024, 1)], "ntn": [], "context": context}

    monkeypatch.setattr(overview, "_fetch_payload", fake)


@pytest.mark.asyncio
async def test_lookup_is_cached_on_disk_for_a_month(monkeypatch, cache_dir):
    calls = []
    _fake_fetch(monkeypatch, calls, context={"income": []})
    first = await overview.load_zone_overview("b696", "d4")
    assert first["zone_found"] and first["cache"]["stale"] is False and first["cache"]["ttl_days"] == 30
    overview._memory.clear()  # a service restart
    second = await overview.load_zone_overview("B696", "D4")
    assert second["zone_found"] and len(calls) == 1
    assert list(cache_dir.glob("B696_D4.json"))


@pytest.mark.asyncio
async def test_expired_copy_is_refetched_and_refresh_skips_the_copy(monkeypatch, cache_dir):
    calls = []
    _fake_fetch(monkeypatch, calls, context={"income": []})
    await overview.load_zone_overview("B696", "D4")
    for payload in overview._memory.values():
        payload["saved_at"] -= 31 * 86400
    await overview.load_zone_overview("B696", "D4")
    await overview.load_zone_overview("B696", "D4", refresh=True)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_month_old_copy_is_still_fresh(monkeypatch, cache_dir):
    calls = []
    _fake_fetch(monkeypatch, calls, context={"income": []})
    await overview.load_zone_overview("B696", "D4")
    for payload in overview._memory.values():
        payload["saved_at"] -= 29 * 86400
    result = await overview.load_zone_overview("B696", "D4")
    assert len(calls) == 1 and result["cache"]["age_days"] == pytest.approx(29, abs=0.1)


@pytest.mark.asyncio
async def test_payload_without_stats_context_is_retried_soon(monkeypatch, cache_dir):
    calls = []
    _fake_fetch(monkeypatch, calls, context=None)
    await overview.load_zone_overview("B696", "D4")
    await overview.load_zone_overview("B696", "D4")
    assert len(calls) == 1  # within the short retry window
    for payload in overview._memory.values():
        payload["saved_at"] -= overview.CONTEXT_RETRY_SECONDS + 1
    await overview.load_zone_overview("B696", "D4")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_expired_copy_is_served_when_the_database_is_down(monkeypatch, cache_dir):
    calls = []
    _fake_fetch(monkeypatch, calls, context={"income": []})
    await overview.load_zone_overview("B696", "D4")
    for payload in overview._memory.values():
        payload["saved_at"] -= 90 * 86400

    async def down(code, zone):
        raise ConnectionRefusedError

    monkeypatch.setattr(overview, "_fetch_payload", down)
    result = await overview.load_zone_overview("B696", "D4")
    assert result["zone_found"] and result["cache"]["stale"] is True


@pytest.mark.asyncio
async def test_unknown_comune_is_not_stored(monkeypatch, cache_dir):
    _fake_fetch(monkeypatch, [], rows=[])
    assert (await overview.load_zone_overview("ZZZZ", "A1")) == {"found": False}
    assert not list(cache_dir.glob("*.json"))


def test_cache_ttl_comes_from_the_environment(monkeypatch):
    from sister.omi_quotes import cache_ttl_seconds

    monkeypatch.delenv("OMI_CACHE_DAYS", raising=False)
    assert cache_ttl_seconds() == 30 * 86400
    monkeypatch.setenv("OMI_CACHE_DAYS", "7")
    assert cache_ttl_seconds() == 7 * 86400
    monkeypatch.setenv("OMI_CACHE_DAYS", "garbage")
    assert cache_ttl_seconds() == 30 * 86400
