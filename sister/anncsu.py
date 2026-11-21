"""Position of a street address from ANNCSU (Archivio Nazionale dei Numeri Civici delle Strade Urbane).

Used by the parcel maps for a parcel the cadastre has no geometry for: the visura gives the address
(``VIA PAZIENZA n. 27-29 Piano T - 1 - 2``), ANNCSU gives the coordinates of the house number, which is
better than a geocoder guess. Data: local ``anncsu`` database, export schema ``anncsu_v2_ref`` (view
``v_address``, the only supported interface); connection ``ANNCSU_POSTGRES_DSN``, otherwise the database part of the cadastral DSN (like ``omi_quotes``).
A missing database, a timeout or no match simply return ``None``: the caller falls back to the geocoder.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from .database_config import database_dsn_suffix

logger = logging.getLogger("sister")

_SQL = """
SELECT a.civic_number, a.exponent, a.latitude, a.longitude, a.street AS odonimo
FROM anncsu_v2_ref.v_address a
WHERE a.cadastral_code = $1 AND upper(a.street) = $2 AND a.civic_number = ANY($3::int[])
  AND a.latitude IS NOT NULL AND a.longitude IS NOT NULL
LIMIT 20
"""
_CACHE: dict[tuple[str, str], dict | None] = {}
_RETRY_AT = 0.0  # after a connection failure the database is not tried again for a while
_RETRY_SECONDS = 60.0


def anncsu_dsn() -> str | None:
    dsn = os.getenv("ANNCSU_POSTGRES_DSN", "").strip()
    if not dsn:
        base = os.getenv("STATS_POSTGRES_DSN", "").strip() or os.getenv("CADASTRAL_POSTGRES_DSN", "").strip()
        if not base:
            suffix = database_dsn_suffix()
            base = os.getenv(f"CADASTRAL_DATABASE_DSN_{suffix}", "").strip()
        if not base:
            return None
        dsn = urlunsplit(urlsplit(base)._replace(path="/anncsu"))
    if not dsn.startswith(("postgres://", "postgresql://", "postgresql+")):
        return None
    return "postgresql://" + dsn.split("://", 1)[1]


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.replace("'", "' ").replace("’", "' ")).strip().upper().replace("' ", "'")


def parse_street_address(address: str) -> tuple[str, list[str]] | None:
    """``VIA PAZIENZA n. 27-29 Piano T - 1 - 2`` → (``VIA PAZIENZA``, [``27``, ``29``]); ``None`` without a civic number."""
    text = _plain(address)
    text = re.sub(r"\b(?:PIANO|P\.|INTERNO|INT\.|SCALA|SC\.|EDIFICIO|PALAZZINA|LOTTO|BLOCCO)\b.*$", "", text).strip(" ,-")
    match = re.match(r"^(?P<street>.*?)[\s,]+(?:N(?:UMERO)?\.?\s*)?(?P<a>\d+)(?:\s*(?:[-/]|E)\s*(?P<b>\d+))?\s*[A-Z]?\b", text)
    if not match:
        return None
    street = match.group("street").strip(" ,.")
    if not street or re.search(r"\bS\.?\s*N\.?\s*C\.?\b", text):
        return None
    civics = [str(int(match.group("a")))]
    if match.group("b") and match.group("b") != match.group("a"):
        civics.append(str(int(match.group("b"))))
    return street, civics


async def anncsu_position(municipality_code: str, address: str, *, timeout: float = 3.0) -> dict | None:
    """``{"lat", "lng", "label"}`` of the house number(s) of ``address`` in the comune, or ``None``."""
    global _RETRY_AT
    code = str(municipality_code or "").strip().upper()
    parsed = parse_street_address(address or "")
    dsn = anncsu_dsn()
    if not (code and parsed and dsn):
        return None
    street, civics = parsed
    key = (code, f"{street}|{','.join(civics)}")
    if key in _CACHE:
        return _CACHE[key]
    if time.monotonic() < _RETRY_AT:
        return None
    try:
        import asyncpg

        connection = await asyncio.wait_for(
            asyncpg.connect(dsn, timeout=timeout, command_timeout=timeout, server_settings={
                "jit": "off", "statement_timeout": str(int(timeout * 1000)), "application_name": "sister-anncsu",
            }),
            timeout=timeout + 1,
        )
        try:
            rows = await connection.fetch(_SQL, code, street, [int(civic) for civic in civics])
        finally:
            await connection.close()
    except Exception as exc:  # noqa: BLE001 - optional source: the geocoder is the fallback
        logger.warning("ANNCSU lookup unavailable: %s", type(exc).__name__)
        _RETRY_AT = time.monotonic() + _RETRY_SECONDS
        return None
    hit = None
    if rows:
        # a "27-29" range covers two house numbers: the point between them; a plain civic number is one point
        latitude = sum(float(row["latitude"]) for row in rows) / len(rows)
        longitude = sum(float(row["longitude"]) for row in rows) / len(rows)
        numbers = [str(number) for number in sorted({row["civic_number"] for row in rows})]
        hit = {"lat": latitude, "lng": longitude, "label": f"{rows[0]['odonimo']} {'-'.join(numbers)}"}
    _CACHE[key] = hit
    return hit
