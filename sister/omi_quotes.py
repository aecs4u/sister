"""Official OMI quotes (Agenzia delle Entrate) read from the local PostgreSQL ``omi`` database, ``public`` schema.

The star-schema tables ``omi_ref_zone`` (one row per zone and semester) and ``omi_fact_quotazioni_valori`` (one row per
zone, typology and conservation state) hold the published quotes. OMI quotes cover buildings only (abitazioni, box,
negozi, uffici, ...): there are no land / agricultural typologies, so nothing here can value terreni.

Connection: ``OMI_POSTGRES_DSN``, otherwise the database part of ``STATS_POSTGRES_DSN`` / ``CADASTRAL_POSTGRES_DSN``
is replaced with ``omi`` (the same convention ``land_registry`` uses for its sibling databases).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .cadastral_values import normalize_category

logger = logging.getLogger("sister")

DEFAULT_CACHE_DAYS = 30.0
_CACHE_MAX_ENTRIES = 512
_FAILURE_BACKOFF_SECONDS = 60.0

# comune (cadastral/administrative code), zone -> (stored at, quotes of the latest published semester)
_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_retry_at = 0.0

# Cadastral category -> OMI typology codes in order of preference. Categories that are missing here (A/4-A/6, A/9,
# B, C/4-C/5, C/7, D, E, F) have no OMI typology that stands for them and are left unvalued rather than guessed.
OMI_TYPOLOGY_BY_CATEGORY: dict[str, tuple[int, ...]] = {
    "A1": (19,),  # Abitazioni signorili
    "A2": (20,),  # Abitazioni civili
    "A3": (21,),  # Abitazioni di tipo economico
    "A7": (1,),  # Ville e Villini
    "A8": (1,),
    "A10": (6,),  # Uffici
    "A11": (22,),  # Abitazioni tipiche dei luoghi
    "C1": (5,),  # Negozi
    "C2": (9,),  # Magazzini
    "C3": (10,),  # Laboratori
    "C6": (13, 16),  # Box, else Autorimesse
}

# Names of the OMI typologies used above, for the /valuation page (tests/test_valuation_docs.py keeps them in step).
OMI_TYPOLOGY_LABELS: dict[int, str] = {
    1: "Ville e villini",
    5: "Negozi",
    6: "Uffici",
    9: "Magazzini",
    10: "Laboratori",
    13: "Box",
    16: "Autorimesse",
    19: "Abitazioni signorili",
    20: "Abitazioni civili",
    21: "Abitazioni di tipo economico",
    22: "Abitazioni tipiche dei luoghi",
}

_QUOTES_SQL = """
SELECT DISTINCT z.anno, z.semestre, z.zona, v.cod_tipologia, v.tipologia, v.stato_conservazione,
       v.stato_prevalente, v.prezzo_min, v.prezzo_max, v.superficie_nl_compravendita
FROM public.omi_ref_zone AS z
JOIN public.omi_fact_quotazioni_valori AS v ON v.zona_id = z.zona_id
WHERE z.cod_comune_amm = $1 AND UPPER(z.zona) = $2
"""

_COMUNE_QUOTES_SQL = """
SELECT DISTINCT z.anno, z.semestre, z.zona, v.cod_tipologia, v.tipologia, v.stato_conservazione,
       v.stato_prevalente, v.prezzo_min, v.prezzo_max, v.superficie_nl_compravendita
FROM public.omi_ref_zone AS z
JOIN public.omi_fact_quotazioni_valori AS v ON v.zona_id = z.zona_id
WHERE z.cod_comune_amm = $1
"""


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


async def _fetch_quotes(key: tuple[str, str], sql: str, args: tuple[str, ...], timeout: float) -> list[dict[str, Any]] | None:
    """Cached query of the ``omi`` database; ``None`` when it cannot be queried (see :func:`omi_zone_quotes`)."""
    global _retry_at
    dsn = omi_dsn()
    if not dsn or time.monotonic() < _retry_at:
        return None
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < cache_ttl_seconds():
        return cached[1]
    try:
        import asyncpg

        connection = await asyncio.wait_for(
            asyncpg.connect(
                dsn,
                timeout=3,
                command_timeout=timeout,
                server_settings={"jit": "off", "statement_timeout": str(int(timeout * 1000))},
            ),
            timeout=4,
        )
        try:
            rows = await connection.fetch(sql, *args)
        finally:
            await connection.close()
    except Exception as exc:  # The estimate is optional: never break the document page.
        _retry_at = time.monotonic() + _FAILURE_BACKOFF_SECONDS
        logger.warning("OMI database lookup failed (%s): %s", key, type(exc).__name__)
        return None
    quotes = _latest_period(rows)
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (time.monotonic(), quotes)
    return quotes


async def omi_zone_quotes(comune_code: str, zone: str, *, timeout: float = 8.0) -> list[dict[str, Any]] | None:
    """Quotes of the latest published semester for one OMI zone.

    ``[]`` means the database was queried and knows no such zone; ``None`` means it could not be queried (not
    configured, unreachable, too slow) so the caller can tell "no quote" from "no data source".
    """
    code, zona = str(comune_code or "").strip().upper(), str(zone or "").strip().upper()
    if not (code and zona):
        return None
    return await _fetch_quotes((code, zona), _QUOTES_SQL, (code, zona), timeout)


async def omi_comune_quotes(comune_code: str, *, timeout: float = 8.0) -> list[dict[str, Any]] | None:
    """Quotes of the latest published semester for every OMI zone of a comune (same row shape as the zone lookup)."""
    code = str(comune_code or "").strip().upper()
    if not code:
        return None
    return await _fetch_quotes((code, "*"), _COMUNE_QUOTES_SQL, (code,), timeout)


def select_quote(quotes: list[dict[str, Any]], category: Any) -> tuple[dict[str, Any] | None, str]:
    """Pick the one OMI quote that stands for a cadastral category, or explain why there is none.

    Among the quotes of a typology the one OMI flags as prevalent is used; without a flag, the single "NORMALE" one.
    Anything still ambiguous is left unvalued instead of guessed.
    """
    candidates = OMI_TYPOLOGY_BY_CATEGORY.get(normalize_category(category))
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
    quotes: list[dict[str, Any]], category: Any
) -> tuple[dict[str, float | int] | None, str]:
    """Return a municipality-wide OMI range when a parcel's exact zone is unknown.

    The lower bound is the lowest selected quote minimum across zones; the upper bound is the highest selected
    quote maximum. This preserves the uncertainty instead of assigning an arbitrary zone to an unmapped parcel.
    """
    if not OMI_TYPOLOGY_BY_CATEGORY.get(normalize_category(category)):
        return None, "Categoria catastale senza una tipologia OMI corrispondente."
    if not quotes:
        return None, "Nessuna quotazione OMI disponibile per il comune."

    quotes_by_zone: dict[str, list[dict[str, Any]]] = {}
    for quote in quotes:
        zone = str(quote.get("zona") or "").strip().upper()
        if zone:
            quotes_by_zone.setdefault(zone, []).append(quote)

    ranges = []
    for zone, zone_quotes in quotes_by_zone.items():
        selected, _reason = select_quote(zone_quotes, category)
        if selected is None:
            continue
        try:
            low = float(selected["prezzo_min"])
            high = float(selected["prezzo_max"])
        except (KeyError, TypeError, ValueError):
            continue
        if low >= 0 and high >= low:
            ranges.append((low, high))

    if not ranges:
        return None, "Tipologia OMI non quotata nelle zone disponibili del comune."
    return {
        "prezzo_min": min(low for low, _high in ranges),
        "prezzo_max": max(high for _low, high in ranges),
        "zone_count": len(ranges),
    }, ""
