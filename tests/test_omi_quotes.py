"""OMI quote selection per cadastral category, DSN derivation and result shaping."""

import asyncio
import weakref
from types import SimpleNamespace

import asyncpg
import pytest

from sister import omi_quotes
from sister.omi_quotes import _latest_period, omi_dsn, select_quote


def quote(code, label, state="NORMALE", prevalent="P", low=1000.0, high=1400.0):
    return {
        "cod_tipologia": code, "tipologia": label, "stato_conservazione": state, "stato_prevalente": prevalent,
        "prezzo_min": low, "prezzo_max": high, "periodo": "2025S1", "superficie": "L",
    }


QUOTES = [quote(20, "Abitazioni civili"), quote(13, "Box", low=1300.0, high=1700.0), quote(5, "Negozi")]


@pytest.mark.parametrize("category, code", [("A02", 20), ("A/2", 20), ("C06", 13), ("C01", 5)])
def test_category_selects_its_typology(category, code):
    chosen, reason = select_quote(QUOTES, category)
    assert chosen["cod_tipologia"] == code and reason == ""


@pytest.mark.parametrize("category", ["A09", "B01", "C04", "D01", "E01", "F01", "", None])
def test_categories_without_an_omi_typology_are_left_unvalued(category):
    chosen, reason = select_quote(QUOTES, category)
    assert chosen is None and "tipologia OMI corrispondente" in reason


def test_missing_typology_in_the_zone_is_reported():
    chosen, reason = select_quote([quote(20, "Abitazioni civili")], "C01")
    assert chosen is None and "non quotata" in reason


def test_garage_falls_back_from_box_to_autorimesse():
    chosen, _ = select_quote([quote(16, "Autorimesse")], "C06")
    assert chosen["cod_tipologia"] == 16


def test_prevalent_state_wins_over_other_states():
    quotes = [quote(20, "Abitazioni civili", state="OTTIMO", prevalent="P", low=2000.0, high=2500.0),
              quote(20, "Abitazioni civili", state="NORMALE", prevalent="", low=1500.0, high=1900.0)]
    chosen, _ = select_quote(quotes, "A02")
    assert chosen["stato_conservazione"] == "OTTIMO"


def test_without_a_prevalent_flag_the_single_normal_state_is_used():
    quotes = [quote(20, "Abitazioni civili", state="OTTIMO", prevalent=""),
              quote(20, "Abitazioni civili", state="NORMALE", prevalent="")]
    chosen, _ = select_quote(quotes, "A02")
    assert chosen["stato_conservazione"] == "NORMALE"


def test_ambiguous_states_are_not_guessed():
    quotes = [quote(20, "Abitazioni civili", state="OTTIMO", prevalent="P"),
              quote(20, "Abitazioni civili", state="NORMALE", prevalent="P")]
    chosen, reason = select_quote(quotes, "A02")
    assert chosen is None and "non determinabile" in reason


def test_latest_period_keeps_only_the_newest_semester_and_normalises_rows():
    def row(anno, semestre, state=" normale "):
        return {"anno": anno, "semestre": semestre, "zona": "D1", "cod_tipologia": 20, "tipologia": "Abitazioni civili",
                "stato_conservazione": state, "stato_prevalente": "P", "prezzo_min": 1.0, "prezzo_max": 2.0,
                "superficie_nl_compravendita": "L"}

    quotes = _latest_period([row(2024, 2), row(2025, 1), row(2024, 1)])
    assert [q["periodo"] for q in quotes] == ["2025S1"]
    assert quotes[0]["stato_conservazione"] == "NORMALE" and quotes[0]["superficie"] == "L"
    assert _latest_period([]) == []


def test_dsn_prefers_the_explicit_setting(monkeypatch):
    monkeypatch.setenv("OMI_POSTGRES_DSN", "postgresql+asyncpg://u@host:5432/mydb?passfile=/x")
    monkeypatch.setenv("STATS_POSTGRES_DSN", "postgresql+asyncpg://u@other/aecs4u-stats")
    assert omi_dsn() == "postgresql://u@host:5432/mydb?passfile=/x"


def test_dsn_is_derived_by_swapping_the_database_name(monkeypatch):
    monkeypatch.delenv("OMI_POSTGRES_DSN", raising=False)
    monkeypatch.setenv("STATS_POSTGRES_DSN", "postgresql+asyncpg://postgres@127.0.0.1:5432/aecs4u-stats?passfile=/p")
    assert omi_dsn() == "postgresql://postgres@127.0.0.1:5432/omi?passfile=/p"


def test_dsn_is_none_when_nothing_is_configured(monkeypatch):
    for name in ("OMI_POSTGRES_DSN", "STATS_POSTGRES_DSN", "CADASTRAL_POSTGRES_DSN"):
        monkeypatch.delenv(name, raising=False)
    assert omi_dsn() is None


async def test_unconfigured_database_means_no_data_source_not_no_quote(monkeypatch):
    for name in ("OMI_POSTGRES_DSN", "STATS_POSTGRES_DSN", "CADASTRAL_POSTGRES_DSN"):
        monkeypatch.delenv(name, raising=False)
    assert await omi_quotes.omi_zone_quotes("G273", "D1") is None


@pytest.fixture
def quote_database(monkeypatch):
    """Track real lookup scheduling without consuming the developer's PostgreSQL slots."""
    monkeypatch.setenv("OMI_POSTGRES_DSN", "postgresql://test/omi")
    monkeypatch.setenv("OMI_CACHE_DAYS", "30")
    monkeypatch.setenv("OMI_MAX_CONNECTIONS", "2")
    monkeypatch.setattr(omi_quotes, "_cache", {})
    monkeypatch.setattr(omi_quotes, "_retry_at", {})
    monkeypatch.setattr(omi_quotes, "_lookup_states", weakref.WeakKeyDictionary())
    db = SimpleNamespace(calls=0, active=0, peak=0, closed=0, fetch=None)

    async def fetch(*args):
        if db.fetch is not None:
            return await db.fetch(*args)
        await asyncio.sleep(0)
        return []

    async def close():
        await asyncio.sleep(0)
        db.active -= 1
        db.closed += 1

    async def connect(*args, **kwargs):
        db.calls += 1
        db.active += 1
        db.peak = max(db.peak, db.active)
        await asyncio.sleep(0)
        return SimpleNamespace(fetch=fetch, close=close)

    monkeypatch.setattr(asyncpg, "connect", connect)
    return db


@pytest.mark.parametrize("zone", ["*", "D1"])
async def test_concurrent_pages_share_lookups_and_limit_connections(quote_database, zone):
    async def lookup(index):
        code = f"K{index % 20:03}"
        if zone == "*":
            return await omi_quotes.omi_comune_quotes(code)
        return await omi_quotes.omi_zone_quotes(code, zone)

    results = await asyncio.gather(*(lookup(index) for index in range(60)))

    assert results == [[]] * 60  # a successful empty result is different from an unavailable database
    assert quote_database.calls == quote_database.closed == 20
    assert quote_database.peak == 2
    assert quote_database.active == 0
    assert not omi_quotes._lookup_state().pending


async def test_capacity_failure_pauses_all_comuni_but_keeps_cached_results(quote_database, monkeypatch, caplog):
    assert await omi_quotes.omi_comune_quotes("CACHED") == []
    now = [100.0]
    monkeypatch.setattr(omi_quotes, "time", SimpleNamespace(monotonic=lambda: now[0]))
    attempts = 0

    async def saturated(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0)
        raise asyncpg.TooManyConnectionsError("too many clients")

    monkeypatch.setattr(asyncpg, "connect", saturated)
    results = await asyncio.gather(*(omi_quotes.omi_comune_quotes(f"K{i:03}") for i in range(60)))

    assert results == [None] * 60
    assert attempts == 2  # already running attempts; queued requests observe the shared backoff
    assert sum("connection limit reached" in record.message for record in caplog.records) == 1
    assert await omi_quotes.omi_comune_quotes("ANOTHER") is None
    assert await omi_quotes.omi_comune_quotes("CACHED") == []
    assert attempts == 2

    now[0] += omi_quotes._FAILURE_BACKOFF_SECONDS + 1
    assert await omi_quotes.omi_comune_quotes("ANOTHER") is None
    assert attempts == 3  # the database is retried when the backoff expires


async def test_identical_inflight_lookups_are_shared_even_without_cache(quote_database, monkeypatch):
    monkeypatch.setenv("OMI_CACHE_DAYS", "0")
    assert await asyncio.gather(*(omi_quotes.omi_comune_quotes("L220") for _ in range(20))) == [[]] * 20
    assert quote_database.calls == 1
    assert await omi_quotes.omi_comune_quotes("L220") == []
    assert quote_database.calls == 2


async def test_cancelling_one_page_preserves_another_pages_lookup(quote_database):
    started, finish = asyncio.Event(), asyncio.Event()

    async def blocked_fetch(*args):
        started.set()
        await finish.wait()
        return []

    quote_database.fetch = blocked_fetch
    first = asyncio.create_task(omi_quotes.omi_comune_quotes("L220"))
    await started.wait()
    second = asyncio.create_task(omi_quotes.omi_comune_quotes("L220"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    finish.set()

    assert await second == []
    assert quote_database.calls == quote_database.closed == 1
    assert not omi_quotes._lookup_state().pending


async def test_cancelling_all_pages_closes_connections_and_discards_queued_work(quote_database):
    started = asyncio.Event()

    async def blocked_fetch(*args):
        started.set()
        await asyncio.Event().wait()

    quote_database.fetch = blocked_fetch
    tasks = [asyncio.create_task(omi_quotes.omi_comune_quotes(f"K{i:03}")) for i in range(20)]
    await started.wait()
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert quote_database.calls == quote_database.closed == 2
    assert quote_database.active == 0
    assert not omi_quotes._lookup_state().pending
    quote_database.fetch = None
    assert await omi_quotes.omi_comune_quotes("NEXT") == []


async def test_failed_query_closes_connection_without_disabling_other_comuni(quote_database):
    async def broken_fetch(*args):
        raise RuntimeError("query failed")

    quote_database.fetch = broken_fetch
    assert await omi_quotes.omi_comune_quotes("BROKEN") is None
    assert quote_database.closed == 1
    quote_database.fetch = None
    assert await omi_quotes.omi_comune_quotes("NEXT") == []
    assert await omi_quotes.omi_comune_quotes("BROKEN") is None
    assert quote_database.calls == quote_database.closed == 2


ALL_RESIDENTIAL = [
    quote(19, "Abitazioni signorili", low=3000.0, high=4000.0), quote(20, "Abitazioni civili", low=2000.0, high=2600.0),
    quote(21, "Abitazioni di tipo economico", low=1500.0, high=1900.0), quote(1, "Ville e Villini", low=2300.0, high=3000.0),
    quote(14, "Posti auto coperti"), quote(15, "Posti auto scoperti", low=500.0, high=700.0),
]


@pytest.mark.parametrize(
    "category, code",
    [("A01", 19), ("A02", 20), ("A03", 20), ("A04", 21), ("A05", 21), ("A06", 21), ("A07", 1), ("A08", 19), ("C07", 14)],
)
def test_residential_categories_use_the_typology_the_agenzia_assigns(category, code):
    chosen, reason = select_quote(ALL_RESIDENTIAL, category)
    assert chosen["cod_tipologia"] == code and reason == ""


def test_a8_falls_back_to_ville_where_signorili_is_not_quoted():
    without_signorili = [q for q in ALL_RESIDENTIAL if q["cod_tipologia"] != 19]

    chosen, _reason = select_quote(without_signorili, "A08")

    assert chosen["cod_tipologia"] == 1  # signorili is quoted in very few comuni; A/1 has no such fallback
    assert select_quote(without_signorili, "A01")[0] is None


def test_c7_falls_back_to_uncovered_parking():
    chosen, _reason = select_quote([q for q in ALL_RESIDENTIAL if q["cod_tipologia"] != 14], "C07")

    assert chosen["cod_tipologia"] == 15
