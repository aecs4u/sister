"""Official OMI quotes (Agenzia delle Entrate) read from the local PostgreSQL ``omi`` database, ``omi`` schema.

The normalised tables ``zone_version`` (one row per zone and semester) and ``quotation_fact`` (one row per zone,
typology and conservation state), with their small dimension tables, hold the published quotes. The legacy
``public.omi_ref_zone`` / ``omi_fact_quotazioni_valori`` copies are no longer read. OMI quotes cover buildings only (abitazioni, box,
negozi, uffici, ...): there are no land / agricultural typologies, so nothing here can value terreni.

Connection: ``OMI_POSTGRES_DSN``, otherwise the database part of ``STATS_POSTGRES_DSN`` / legacy
``CADASTRAL_POSTGRES_DSN`` is replaced with ``omi``. When those are unset, the selected
``CADASTRAL_DATABASE_DSN_LOCALHOST`` / ``CADASTRAL_DATABASE_DSN_NEON`` is used according to ``ENVIRONMENT``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import weakref
from dataclasses import dataclass, field
from statistics import median
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .cadastral_values import normalize_category

logger = logging.getLogger("sister")

DEFAULT_CACHE_DAYS = 30.0
_CACHE_MAX_ENTRIES = 512
_FAILURE_BACKOFF_SECONDS = 60.0
_DEFAULT_MAX_CONNECTIONS = 2

# comune (cadastral/administrative code), zone -> (stored at, quotes of the latest published semester)
_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_retry_at: dict[tuple[str, str], float] = {}


@dataclass
class _PendingLookup:
    task: asyncio.Task
    waiters: int = 0


@dataclass
class _LookupState:
    # Shared by all document requests on this worker, rather than one gate per page.
    gate: asyncio.Semaphore
    pending: dict[tuple[str, str], _PendingLookup] = field(default_factory=dict)
    retry_at: float = 0.0


_lookup_states: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _lookup_state() -> _LookupState:
    loop = asyncio.get_running_loop()
    state = _lookup_states.get(loop)
    if state is None:
        try:
            limit = max(1, int(os.getenv("OMI_MAX_CONNECTIONS", str(_DEFAULT_MAX_CONNECTIONS))))
        except ValueError:
            limit = _DEFAULT_MAX_CONNECTIONS
        state = _LookupState(asyncio.Semaphore(limit))
        _lookup_states[loop] = state
    return state


# Cadastral category -> OMI typology codes in order of preference.
#
# Residential categories follow the Agenzia delle Entrate's own correspondence (Manuale OMI 2025, allegati, Tabella 1):
# signorili = A/1 and A/8, civili = A/2-A/3, economiche = A/4-A/6, ville e villini = A/7, tipiche dei luoghi = A/11,
# box e posti auto = C/6 and C/7. Two deliberate additions: A/8 falls back to ville e villini (signorili is quoted in
# only ~65 comuni) and C/7 (tettoie) is valued as a covered, else uncovered, parking space.
# Everything else (A/9, B, C/4-C/5, D, E, F) has no OMI typology that stands for it and is left unvalued, not guessed. The
# Agency's own caps (C/2 up to 30 m2 and C/6-C/7 up to 50 m2 are *pertinenze*, valued as a share of the house) are not applied.
OMI_TYPOLOGY_BY_CATEGORY: dict[str, tuple[int, ...]] = {
    "A1": (19,),  # Abitazioni signorili
    "A2": (20,),  # Abitazioni civili
    "A3": (20,),  # Abitazioni civili (A/2-A/3), not economico
    "A4": (21,),  # Abitazioni di tipo economico (A/4-A/6)
    "A5": (21,),
    "A6": (21,),
    "A7": (1,),  # Ville e Villini
    "A8": (19, 1),  # Abitazioni signorili (A/1, A/8), else Ville e villini
    "A10": (6,),  # Uffici
    "A11": (22,),  # Abitazioni tipiche dei luoghi
    "C1": (5,),  # Negozi
    "C2": (9,),  # Magazzini
    "C3": (10,),  # Laboratori
    "C6": (13, 16),  # Box, else Autorimesse
    "C7": (14, 15),  # Posti auto coperti, else scoperti
}

# Names of the OMI typologies used above, for the /valuation page (tests/test_valuation_docs.py keeps them in step).
OMI_TYPOLOGY_LABELS: dict[int, str] = {
    1: "Ville e villini",
    5: "Negozi",
    6: "Uffici",
    9: "Magazzini",
    10: "Laboratori",
    13: "Box",
    14: "Posti auto coperti",
    15: "Posti auto scoperti",
    16: "Autorimesse",
    19: "Abitazioni signorili",
    20: "Abitazioni civili",
    21: "Abitazioni di tipo economico",
    22: "Abitazioni tipiche dei luoghi",
}

_QUOTES_SQL = """
WITH zz AS MATERIALIZED (
    SELECT p.year AS anno, p.semester AS semestre, z.zone_version_id AS zona_id, z.zone_name AS zona,
           z.market_band AS fascia, z.zone_description AS zona_descrizione
    FROM omi.zone_municipality zm
    JOIN omi.zone_version z ON z.zone_municipality_id = zm.zone_municipality_id
    JOIN omi.period p ON p.period_id = z.period_id
    LEFT JOIN omi.municipality_code istat ON istat.municipality_code_id = zm.istat_code_id
    LEFT JOIN omi.municipality_code cadastral ON cadastral.municipality_code_id = zm.cadastral_code_id
    LEFT JOIN omi.municipality_code administrative ON administrative.municipality_code_id = zm.administrative_code_id
    WHERE (administrative.code_value = $1 OR cadastral.code_value = $1
           OR ($3::float8 IS NOT NULL AND istat.code_value = ($3::float8)::bigint::text))
      AND UPPER(z.zone_name) = $2
),
latest AS (
    SELECT anno, semestre FROM zz ORDER BY anno DESC, semestre DESC LIMIT 1
)
SELECT DISTINCT zz.anno, zz.semestre, zz.zona, zz.fascia, zz.zona_descrizione, pt.source_code AS cod_tipologia,
       pt.type_name AS tipologia, cs.status_code AS stato_conservazione, ps.state_code AS stato_prevalente,
       f.purchase_min::float8 AS prezzo_min, f.purchase_max::float8 AS prezzo_max,
       sp.class_code AS superficie_nl_compravendita
FROM zz
JOIN latest ON latest.anno = zz.anno AND latest.semestre = zz.semestre
JOIN omi.quotation_fact f ON f.zone_version_id = zz.zona_id
LEFT JOIN omi.property_type pt ON pt.property_type_id = f.property_type_id
LEFT JOIN omi.conservation_status cs ON cs.conservation_status_id = f.conservation_status_id
LEFT JOIN omi.prevalent_state ps ON ps.prevalent_state_id = f.prevalent_state_id
LEFT JOIN omi.surface_class sp ON sp.surface_class_id = f.purchase_surface_id
"""

_COMUNE_QUOTES_SQL = """
WITH zz AS MATERIALIZED (
    SELECT p.year AS anno, p.semester AS semestre, z.zone_version_id AS zona_id, z.zone_name AS zona,
           z.market_band AS fascia, z.zone_description AS zona_descrizione
    FROM omi.zone_municipality zm
    JOIN omi.zone_version z ON z.zone_municipality_id = zm.zone_municipality_id
    JOIN omi.period p ON p.period_id = z.period_id
    LEFT JOIN omi.municipality_code istat ON istat.municipality_code_id = zm.istat_code_id
    LEFT JOIN omi.municipality_code cadastral ON cadastral.municipality_code_id = zm.cadastral_code_id
    LEFT JOIN omi.municipality_code administrative ON administrative.municipality_code_id = zm.administrative_code_id
    WHERE (administrative.code_value = $1 OR cadastral.code_value = $1
           OR ($2::float8 IS NOT NULL AND istat.code_value = ($2::float8)::bigint::text))
),
latest AS (
    SELECT anno, semestre FROM zz ORDER BY anno DESC, semestre DESC LIMIT 1
)
SELECT DISTINCT zz.anno, zz.semestre, zz.zona, zz.fascia, zz.zona_descrizione, pt.source_code AS cod_tipologia,
       pt.type_name AS tipologia, cs.status_code AS stato_conservazione, ps.state_code AS stato_prevalente,
       f.purchase_min::float8 AS prezzo_min, f.purchase_max::float8 AS prezzo_max,
       sp.class_code AS superficie_nl_compravendita
FROM zz
JOIN latest ON latest.anno = zz.anno AND latest.semestre = zz.semestre
JOIN omi.quotation_fact f ON f.zone_version_id = zz.zona_id
LEFT JOIN omi.property_type pt ON pt.property_type_id = f.property_type_id
LEFT JOIN omi.conservation_status cs ON cs.conservation_status_id = f.conservation_status_id
LEFT JOIN omi.prevalent_state ps ON ps.prevalent_state_id = f.prevalent_state_id
LEFT JOIN omi.surface_class sp ON sp.surface_class_id = f.purchase_surface_id
"""


# OMI "fascia" (band) letters: where in the municipality a zone lies (Manuale della Banca Dati Quotazioni OMI).
OMI_ZONE_BANDS = {"B": "Centrale", "C": "Semicentrale", "D": "Periferica", "E": "Suburbana", "R": "Extraurbana / rurale"}


_SMALL_WORDS = {"a", "al", "alla", "alle", "ai", "agli", "da", "dal", "dalla", "di", "del", "della", "delle", "dei", "degli",
                "e", "ed", "il", "lo", "la", "le", "i", "gli", "in", "nel", "nella", "nelle", "su", "sul", "sulla", "per", "con"}


def _title_it(value: str) -> str:
    """OMI writes zone descriptions in capitals: "FRAZIONI A SUD DEL CAPOLUOGO" -> "Frazioni a sud del Capoluogo"."""
    words = " ".join(str(value or "").split()).lower().split(" ")
    out = []
    for index, word in enumerate(words):
        if not word:
            continue
        if index and word in _SMALL_WORDS:
            out.append(word)
        else:
            out.append("-".join(part[:1].upper() + part[1:] for part in word.split("-")))
    return " ".join(out)


def zone_description(zone: str, description: str = "", band: str = "") -> str:
    """Self-explanatory zone label: ``"R1 · Residuale montana (Extraurbana / rurale)"``; just the code when OMI gives none."""
    zone = str(zone or "").strip().upper()
    text = _title_it(description)
    band_name = OMI_ZONE_BANDS.get(str(band or "").strip().upper(), "")
    label = f"{zone} · {text}" if text else zone
    return f"{label} ({band_name})" if band_name and zone else label


def cache_ttl_seconds() -> float:
    """How long OMI data is reused: the quotes change once per semester, so a month by default (``OMI_CACHE_DAYS``)."""
    try:
        days = float(os.getenv("OMI_CACHE_DAYS", "").strip() or DEFAULT_CACHE_DAYS)
    except ValueError:
        days = DEFAULT_CACHE_DAYS
    return max(days, 0.0) * 86400.0


def omi_dsn() -> str | None:
    """asyncpg DSN of the ``omi`` database, or ``None`` when it is not configured."""
    dsn = os.getenv("OMI_POSTGRES_DSN", "").strip()
    if not dsn:
        base = os.getenv("STATS_POSTGRES_DSN", "").strip() or os.getenv("CADASTRAL_POSTGRES_DSN", "").strip()
        if not base:
            environment = os.getenv("ENVIRONMENT", "development").strip().lower()
            suffix = "NEON" if environment in {"production", "prod", "staging"} else "LOCALHOST"
            base = os.getenv(f"CADASTRAL_DATABASE_DSN_{suffix}", "").strip()
        if not base:
            return None
        dsn = urlunsplit(urlsplit(base)._replace(path="/omi"))
    if not dsn.startswith(("postgres://", "postgresql://", "postgresql+")):
        return None
    return "postgresql://" + dsn.split("://", 1)[1]  # asyncpg does not know the ``+asyncpg`` driver suffix


def _latest_period(rows: list[Any]) -> list[dict[str, Any]]:
    """Keep the newest published semester and normalise the row shape."""
    if not rows:
        return []
    newest = max((row["anno"] or 0) * 10 + (row["semestre"] or 0) for row in rows)
    quotes = []
    for row in rows:
        if (row["anno"] or 0) * 10 + (row["semestre"] or 0) != newest:
            continue
        quotes.append({
            "zona": row["zona"],
            "fascia": (row.get("fascia") or "").strip().upper(),
            "zona_descrizione": " ".join(str(row.get("zona_descrizione") or "").split()),
            "periodo": f"{row['anno']}S{row['semestre']}",
            "cod_tipologia": row["cod_tipologia"],
            "tipologia": row["tipologia"],
            "stato_conservazione": (row["stato_conservazione"] or "").strip().upper(),
            "stato_prevalente": (row["stato_prevalente"] or "").strip().upper(),
            "prezzo_min": row["prezzo_min"],
            "prezzo_max": row["prezzo_max"],
            "superficie": (row["superficie_nl_compravendita"] or "").strip().upper(),
        })
    return quotes


def _istat_code_parameter(code: str) -> int | None:
    """Numeric code parameter for the OMI ISTAT key (stored as float8 in the imported OMI reference table)."""
    return int(code) if code.isascii() and code.isdigit() and 5 <= len(code) <= 6 else None


async def _fetch_quotes(
    key: tuple[str, str], sql: str, args: tuple[Any, ...], timeout: float
) -> list[dict[str, Any]] | None:
    """Cached query of the ``omi`` database; ``None`` when it cannot be queried (see :func:`omi_zone_quotes`)."""
    dsn = omi_dsn()
    now = time.monotonic()
    if not dsn:
        return None
    cached = _cache.get(key)
    if cached and now - cached[0] < cache_ttl_seconds():
        return cached[1]
    state = _lookup_state()
    if now < max(_retry_at.get(key, 0.0), state.retry_at):
        return None
    pending = state.pending.get(key)
    if pending is None:
        pending = _PendingLookup(asyncio.create_task(_query_quotes(state, dsn, key, sql, args, timeout)))
        state.pending[key] = pending
    pending.waiters += 1
    try:
        # A page timing out must not cancel a lookup another page is still awaiting.
        return await asyncio.shield(pending.task)
    finally:
        pending.waiters -= 1
        if not pending.waiters:
            state.pending.pop(key, None)
            if not pending.task.done():
                pending.task.cancel()
                await asyncio.gather(pending.task, return_exceptions=True)


async def _query_quotes(
    state: _LookupState, dsn: str, key: tuple[str, str], sql: str, args: tuple[Any, ...], timeout: float
) -> list[dict[str, Any]] | None:
    async with state.gate:
        # Queued requests must observe a capacity failure from an earlier lookup.
        if time.monotonic() < state.retry_at:
            return None
        return await _query_quotes_connected(state, dsn, key, sql, args, timeout)


async def _query_quotes_connected(
    state: _LookupState, dsn: str, key: tuple[str, str], sql: str, args: tuple[Any, ...], timeout: float
) -> list[dict[str, Any]] | None:
    try:
        import asyncpg

        connection = await asyncio.wait_for(
            asyncpg.connect(
                dsn,
                timeout=3,
                command_timeout=timeout,
                server_settings={
                    "jit": "off", "statement_timeout": str(int(timeout * 1000)),
                    "application_name": "sister-omi-quotes",
                },
            ),
            timeout=4,
        )
        try:
            rows = await connection.fetch(sql, *args)
        finally:
            await connection.close()
    except Exception as exc:  # The estimate is optional: never break the document page.
        now = time.monotonic()
        _retry_at[key] = now + _FAILURE_BACKOFF_SECONDS
        if len(_retry_at) > _CACHE_MAX_ENTRIES:
            _retry_at.pop(next(iter(_retry_at)))
        if getattr(exc, "sqlstate", None) == "53300":  # PostgreSQL too_many_connections, shared across comuni.
            if now >= state.retry_at:
                logger.warning("OMI database connection limit reached; pausing lookups for %.0fs", _FAILURE_BACKOFF_SECONDS)
            state.retry_at = now + _FAILURE_BACKOFF_SECONDS
        else:
            logger.warning("OMI database lookup failed (%s): %s", key, type(exc).__name__)
        return None
    _retry_at.pop(key, None)
    quotes = _latest_period(rows)
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (time.monotonic(), quotes)
    return quotes


async def omi_zone_quotes(comune_code: str, zone: str, *, timeout: float = 8.0) -> list[dict[str, Any]] | None:
    """Quotes of the latest published semester for one OMI zone.

    ``comune_code`` may be the OMI administrative code, cadastral code or ISTAT code.
    ``[]`` means the database was queried and knows no such zone; ``None`` means it could not be queried (not
    configured, unreachable, too slow) so the caller can tell "no quote" from "no data source".
    """
    code, zona = str(comune_code or "").strip().upper(), str(zone or "").strip().upper()
    if not (code and zona):
        return None
    return await _fetch_quotes(
        (code, zona), _QUOTES_SQL, (code, zona, _istat_code_parameter(code)), timeout
    )


async def omi_comune_quotes(comune_code: str, *, timeout: float = 8.0) -> list[dict[str, Any]] | None:
    """Latest quotes for every zone, addressed by OMI administrative, cadastral or ISTAT comune code."""
    code = str(comune_code or "").strip().upper()
    if not code:
        return None
    return await _fetch_quotes((code, "*"), _COMUNE_QUOTES_SQL, (code, _istat_code_parameter(code)), timeout)


def select_quote(
    quotes: list[dict[str, Any]], category: Any, *, typology_code: int | None = None
) -> tuple[dict[str, Any] | None, str]:
    """Pick the one OMI quote that stands for a cadastral category, or explain why there is none.

    Among the quotes of a typology the one OMI flags as prevalent is used; without a flag, the single "NORMALE" one.
    Anything still ambiguous is left unvalued instead of guessed.
    """
    candidates = (typology_code,) if typology_code is not None else OMI_TYPOLOGY_BY_CATEGORY.get(normalize_category(category))
    if not candidates:
        return None, "Categoria catastale senza una tipologia OMI corrispondente."
    ambiguous = False
    for code in candidates:
        rows = [
            quote for quote in quotes
            if str(quote.get("cod_tipologia", "")).strip() == str(code)
        ]
        if not rows:
            continue
        chosen = [quote for quote in rows if quote.get("stato_prevalente") == "P"] or [
            quote for quote in rows if quote.get("stato_conservazione") == "NORMALE"
        ]
        if len(chosen) == 1:
            return chosen[0], ""
        ambiguous = True
    if ambiguous:
        return None, "Stato di conservazione OMI non determinabile in modo univoco."
    return None, "Tipologia OMI non quotata in questa zona."


def select_quote_range_across_zones(
    quotes: list[dict[str, Any]], category: Any, *, typology_code: int | None = None
) -> tuple[dict[str, float | int] | None, str]:
    """Return a municipality-wide OMI range when a parcel's exact zone is unknown.

    The lower bound is the lowest selected quote minimum across zones; the upper bound is the highest selected
    quote maximum. A representative price is the median of the per-zone quote midpoints, so an unmapped parcel does not
    get a central estimate formed from the two municipality-wide extremes.
    """
    if typology_code is None and not OMI_TYPOLOGY_BY_CATEGORY.get(normalize_category(category)):
        return None, "Categoria catastale senza una tipologia OMI corrispondente."
    if not quotes:
        return None, "Nessuna quotazione OMI disponibile per il comune."

    quotes_by_zone: dict[str, list[dict[str, Any]]] = {}
    for quote in quotes:
        zone = str(quote.get("zona") or "").strip().upper()
        if zone:
            quotes_by_zone.setdefault(zone, []).append(quote)

    ranges = []
    midpoints = []
    selected_codes: list[int] = []
    for zone, zone_quotes in quotes_by_zone.items():
        selected, _reason = select_quote(zone_quotes, category, typology_code=typology_code)
        if selected is None:
            continue
        try:
            low = float(selected["prezzo_min"])
            high = float(selected["prezzo_max"])
        except (KeyError, TypeError, ValueError):
            continue
        if low >= 0 and high >= low:
            ranges.append((low, high))
            midpoints.append((low + high) / 2)
            try:
                selected_codes.append(int(selected["cod_tipologia"]))
            except (KeyError, TypeError, ValueError):
                pass

    if not ranges:
        return None, "Tipologia OMI non quotata nelle zone disponibili del comune."
    counts = {code: selected_codes.count(code) for code in set(selected_codes)}
    category_order = OMI_TYPOLOGY_BY_CATEGORY.get(normalize_category(category), ())
    chosen_code = min(
        counts,
        key=lambda code: (-counts[code], category_order.index(code) if code in category_order else len(category_order), code),
    ) if counts else typology_code
    return {
        "prezzo_min": min(low for low, _high in ranges),
        "prezzo_max": max(high for _low, high in ranges),
        # A parcel with no exact zone still gets a representative price based on the published
        # zone bands, rather than the midpoint of two municipality-wide extremes.
        "prezzo_mid": median(midpoints),
        "zone_count": len(ranges),
        "cod_tipologia": chosen_code,
    }, ""
