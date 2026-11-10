"""Asking-price €/m² from the local ``immobiliare`` and ``idealista`` databases, as a market reference next to OMI.

* **Immobiliare.it** (``immobiliare_source_valuation_current``): sale €/m² per town (the rows without a ``zone``); the
  data is an extract (see ``as_of``), not live. Geography is a slug (``palermo``), matched on the comune name.
* **idealista** (``idealista_listing_observations``): the sale listings of the latest observation day with their own
  €/m²; the quartiles of the listings within ``RADIUS_KM`` of the parcel (when it has a position and at least
  ``MIN_NEARBY`` listings are there), otherwise of the whole comune.

Both are *asking* prices of the listings the databases hold, usually above transaction prices, and cover only the
comuni that were collected. Connection: ``IMMOBILIARE_POSTGRES_DSN`` / ``IDEALISTA_POSTGRES_DSN``, otherwise the
database part of ``OPENDATA_DATABASE_DSN`` is replaced. Nothing here may break a page: every failure is ``None``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import unicodedata
from typing import Any
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("sister")

RADIUS_KM = 1.5
MIN_NEARBY = 5
_CACHE_SECONDS = 3600.0
_RETRY_SECONDS = 60.0
_cache: dict[tuple, tuple[float, Any]] = {}
_retry_at: dict[str, float] = {}


def _dsn(env_name: str, database: str) -> str | None:
    dsn = os.getenv(env_name, "").strip()
    if not dsn:
        base = os.getenv("OPENDATA_DATABASE_DSN", "").strip()
        if not base:
            return None
        dsn = urlunsplit(urlsplit(base)._replace(path="/" + database))
    if not dsn.startswith(("postgres://", "postgresql://", "postgresql+")):
        return None
    return "postgresql://" + dsn.split("://", 1)[1]  # asyncpg does not know the ``+asyncpg`` suffix


def slug(name: str) -> str:
    """``Canicattì`` → ``canicatti``, ``Lampedusa e Linosa`` → ``lampedusa-e-linosa`` (Immobiliare's town slugs)."""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


async def _fetch(database: str, env_name: str, sql: str, args: tuple, timeout: float = 6.0) -> list[Any] | None:
    dsn = _dsn(env_name, database)
    if not dsn or time.monotonic() < _retry_at.get(database, 0.0):
        return None
    try:
        import asyncpg

        connection = await asyncio.wait_for(
            asyncpg.connect(dsn, timeout=3, command_timeout=timeout,
                            server_settings={"statement_timeout": str(int(timeout * 1000))}),
            timeout=4,
        )
        try:
            return list(await connection.fetch(sql, *args))
        finally:
            await connection.close()
    except Exception as exc:  # noqa: BLE001 - optional reference data
        _retry_at[database] = time.monotonic() + _RETRY_SECONDS
        logger.warning("%s database lookup failed: %s", database, type(exc).__name__)
        return None


async def _cached(key: tuple, producer):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    value = await producer()
    if value is not None:
        if len(_cache) > 512:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (time.monotonic(), value)
    return value


_IMMOBILIARE_SQL = """
SELECT sale_eur_sqm, extracted_at
FROM immobiliare_source_valuation_current
WHERE town = $1 AND zone IS NULL AND sale_eur_sqm > 0
  AND ($2::text = '' OR province LIKE $2 || '%')
  AND extracted_at = (SELECT max(extracted_at) FROM immobiliare_source_valuation_current WHERE town = $1 AND zone IS NULL)
"""


async def immobiliare_quote(comune: str, province: str = "") -> dict[str, Any] | None:
    """Sale €/m² low / middle / high of a comune on Immobiliare.it, or ``None`` when it is not in the extract."""
    town = slug(comune)
    if not town:
        return None

    async def produce():
        rows = await _fetch("immobiliare", "IMMOBILIARE_POSTGRES_DSN", _IMMOBILIARE_SQL, (town, slug(province)))
        if not rows:
            return None
        values = sorted(float(row["sale_eur_sqm"]) for row in rows)
        return {
            "source": "immobiliare", "label": "Immobiliare.it", "low": values[0], "mid": values[len(values) // 2],
            "high": values[-1], "scope": f"comune di {comune.title()}", "as_of": rows[0]["extracted_at"].date().isoformat(),
        }

    return await _cached(("immobiliare", town, slug(province)), produce)


_IDEALISTA_SQL = """
WITH latest AS (SELECT max(observed_at)::date AS day FROM idealista_listing_observations),
base AS (
    SELECT o.price_per_sqm::float8 AS p, o.latitude, o.longitude, o.observed_at
    FROM idealista_listing_observations o
    JOIN idealista_locations l ON l.id = o.location_ref, latest
    WHERE lower(l.city) = lower($1) AND o.transaction_type = 'sale' AND o.price_per_sqm > 0
      AND coalesce(o.property_type, '') <> 'countryHouse' AND o.observed_at::date = latest.day
),
near AS (
    SELECT p FROM base
    WHERE $2::float8 IS NOT NULL AND latitude IS NOT NULL
      AND 6371 * 2 * asin(sqrt(power(sin(radians(latitude - $2) / 2), 2)
          + cos(radians($2)) * cos(radians(latitude)) * power(sin(radians(longitude - $3) / 2), 2))) <= $4
)
SELECT 'near' AS scope, count(*) AS n, percentile_cont(0.25) WITHIN GROUP (ORDER BY p) AS q1,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY p) AS q2, percentile_cont(0.75) WITHIN GROUP (ORDER BY p) AS q3,
       NULL::date AS day FROM near
UNION ALL
SELECT 'comune', count(*), percentile_cont(0.25) WITHIN GROUP (ORDER BY p),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY p), percentile_cont(0.75) WITHIN GROUP (ORDER BY p),
       max(observed_at)::date FROM base
"""


async def idealista_quote(comune: str, latitude: float | None = None, longitude: float | None = None) -> dict[str, Any] | None:
    """Quartiles of the sale €/m² of the idealista listings near a point, else in the whole comune."""
    if not comune:
        return None
    point = (round(latitude, 3), round(longitude, 3)) if latitude is not None and longitude is not None else (None, None)

    async def produce():
        rows = await _fetch("idealista", "IDEALISTA_POSTGRES_DSN", _IDEALISTA_SQL, (comune, point[0], point[1], RADIUS_KM))
        if not rows:
            return None
        by_scope = {row["scope"]: row for row in rows}
        near, whole = by_scope.get("near"), by_scope.get("comune")
        if not whole or not whole["n"]:
            return None
        chosen, scope = (near, f"entro {str(RADIUS_KM).replace('.', ',')} km") if near and near["n"] >= MIN_NEARBY else (
            whole, f"comune di {comune.title()}")
        return {
            "source": "idealista", "label": "idealista", "low": float(chosen["q1"]), "mid": float(chosen["q2"]),
            "high": float(chosen["q3"]), "n": int(chosen["n"]), "scope": scope, "as_of": whole["day"].isoformat(),
        }

    return await _cached(("idealista", comune.casefold(), point), produce)
