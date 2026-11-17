"""Backfill persistent parcel points from cadastral polygons and ANNCSU addresses.

Run after ``alembic upgrade head`` with the selected SISTER database configured.
The job is resumable: cadastral geometry is preferred and ANNCSU is used only
for building parcels whose address and municipality/civic number match exactly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import tuple_row
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env", override=False)

_COORDINATE_UPSERT = """\
INSERT INTO sister.cadastral_parcel_coordinate
    (cadastral_code, section, sheet, parcel, latitude, longitude,
     location_source, source_reference, source_method, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (cadastral_code, section, sheet, parcel) DO UPDATE SET
    latitude = EXCLUDED.latitude,
    longitude = EXCLUDED.longitude,
    location_source = EXCLUDED.location_source,
    source_reference = EXCLUDED.source_reference,
    source_method = EXCLUDED.source_method,
    updated_at = now()
WHERE CASE sister.cadastral_parcel_coordinate.location_source
          WHEN 'cadastral_geometry' THEN 3
          WHEN 'anncsu_address' THEN 2
          ELSE 1
      END <= CASE EXCLUDED.location_source
                 WHEN 'cadastral_geometry' THEN 3
                 WHEN 'anncsu_address' THEN 2
                 ELSE 1
             END
"""

_REFERENCE_QUERY = """\
SELECT DISTINCT ON (
           upper(btrim(m.cadastral_code)), upper(btrim(coalesce(cu.section, ''))),
           upper(btrim(cu.sheet)), upper(btrim(cu.parcel))
       )
       upper(btrim(m.cadastral_code)) AS cadastral_code,
       coalesce(upper(btrim(cu.section)), '') AS section,
       btrim(cu.sheet) AS sheet,
       btrim(cu.parcel) AS parcel,
       coalesce(nullif(btrim(m.italian_name), ''), m.name) AS municipality,
       coalesce(m.province_abbreviation, '') AS province
FROM sister.cadastral_unit cu
JOIN sister.municipalities m ON m.numeric_code = cu.comune_id
WHERE nullif(btrim(m.cadastral_code), '') IS NOT NULL
  AND nullif(btrim(cu.sheet), '') IS NOT NULL
  AND nullif(btrim(cu.parcel), '') IS NOT NULL
ORDER BY upper(btrim(m.cadastral_code)),
         upper(btrim(coalesce(cu.section, ''))), upper(btrim(cu.sheet)), upper(btrim(cu.parcel)),
         cu.id
"""

_ADDRESS_QUERY = """\
WITH address_candidates AS (
    SELECT p.cadastral_unit_id, p.address, NULL::text AS civic_number
    FROM sister.property p
    WHERE nullif(btrim(p.address), '') IS NOT NULL

    UNION ALL

    SELECT bu.cadastral_unit_id, bu.address_text, NULL::text
    FROM sister.building_unit bu
    WHERE bu.is_current AND nullif(btrim(bu.address_text), '') IS NOT NULL

    UNION ALL

    SELECT h.cadastral_unit_id, h.address, NULL::text
    FROM sister.property_state_history h
    WHERE h.ended_at IS NULL AND nullif(btrim(h.address), '') IS NOT NULL

    UNION ALL

    SELECT mip.cadastral_unit_id, mip.address, mip.civic_number
    FROM sister.mortgage_inspection_property mip
    WHERE nullif(btrim(mip.address), '') IS NOT NULL
)
SELECT DISTINCT
       upper(btrim(m.cadastral_code)) AS cadastral_code,
       coalesce(upper(btrim(cu.section)), '') AS section,
       btrim(cu.sheet) AS sheet,
       btrim(cu.parcel) AS parcel,
       coalesce(nullif(btrim(m.italian_name), ''), m.name) AS municipality,
       coalesce(m.province_abbreviation, '') AS province,
       btrim(a.address) AS address,
       nullif(btrim(a.civic_number), '') AS civic_number
FROM address_candidates a
JOIN sister.cadastral_unit cu ON cu.id = a.cadastral_unit_id
JOIN sister.municipalities m ON m.numeric_code = cu.comune_id
WHERE cu.cadastre_type = 'F'
  AND nullif(btrim(m.cadastral_code), '') IS NOT NULL
  AND nullif(btrim(cu.sheet), '') IS NOT NULL
  AND nullif(btrim(cu.parcel), '') IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM sister.cadastral_parcel_coordinate pc
      WHERE pc.cadastral_code = upper(btrim(m.cadastral_code))
        AND pc.section = coalesce(upper(btrim(cu.section)), '')
        AND pc.sheet = btrim(cu.sheet)
        AND pc.parcel = btrim(cu.parcel)
        AND pc.location_source <> 'osm_address'
  )
"""

_ANNCSU_COVERAGE_CODES = """\
SELECT DISTINCT upper(btrim(comune_code))
FROM anncsu.r19__addresses
WHERE nullif(btrim(comune_code), '') IS NOT NULL
"""

_ANNCSU_MATCH_QUERY = """\
WITH wanted AS (
    SELECT DISTINCT upper(btrim(cadastral_code)) AS comune_code,
                    upper(btrim(street_name)) AS street_name,
                    btrim(civic_number) AS civic_number
    FROM unnest(%s::text[], %s::text[], %s::text[])
         AS requested(cadastral_code, street_name, civic_number)
)
SELECT w.comune_code, w.street_name, w.civic_number,
       a.access_id, a.latitude, a.longitude, a.coordinate_method, s.odonimo
FROM wanted w
JOIN anncsu.r19__addresses a
  ON upper(btrim(a.comune_code)) = w.comune_code
 AND btrim(a.civic_number) = w.civic_number
JOIN anncsu.r19__streets s
  ON s.street_id = a.street_id
 AND upper(btrim(s.comune_code)) = w.comune_code
WHERE upper(translate(s.odonimo,
      'ÀÁÂÃÄÅÈÉÊËÌÍÎÏÒÓÔÕÖÙÚÛÜ',
      'AAAAAAEEEEIIIIOOOOOUUUU')) = w.street_name
  AND a.latitude IS NOT NULL AND a.longitude IS NOT NULL
ORDER BY w.comune_code, w.street_name, w.civic_number, a.access_id
"""
_UPSERT_COORDINATE = """\
INSERT INTO sister.cadastral_parcel_coordinate
    (cadastral_code, section, sheet, parcel, latitude, longitude,
     location_source, source_reference, source_method, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, 'anncsu_address', %s, %s, now())
ON CONFLICT (cadastral_code, section, sheet, parcel) DO UPDATE SET
    latitude = EXCLUDED.latitude,
    longitude = EXCLUDED.longitude,
    location_source = EXCLUDED.location_source,
    source_reference = EXCLUDED.source_reference,
    source_method = EXCLUDED.source_method,
    updated_at = now()
WHERE sister.cadastral_parcel_coordinate.location_source = 'osm_address'
   OR sister.cadastral_parcel_coordinate.location_source = 'anncsu_address'
"""


def _distance_m(point_a: tuple[float, float], point_b: tuple[float, float]) -> float:
    import math

    lon_a, lat_a = point_a
    lon_b, lat_b = point_b
    lat_delta = math.radians(lat_b - lat_a)
    lon_delta = math.radians(lon_b - lon_a)
    h = math.sin(lat_delta / 2) ** 2 + math.cos(math.radians(lat_a)) * math.cos(math.radians(lat_b)) * math.sin(lon_delta / 2) ** 2
    return 6_371_000 * 2 * math.asin(min(1.0, math.sqrt(h)))


async def _connection(dsn: str, schema: str, timeout_ms: int = 30_000) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(
        dsn,
        row_factory=tuple_row,
        connect_timeout=5,
        options=f"-c search_path={schema} -c statement_timeout={timeout_ms}",
    )


def _psycopg_dsn(value: str) -> str:
    url = make_url(value)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("The parcel geocoding backfill requires PostgreSQL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _anncsu_dsn(sister_dsn: str) -> str | None:
    from sister.anncsu import anncsu_dsn

    configured = os.getenv("SISTER_ANNCSU_POSTGRES_DSN", "").strip()
    if configured:
        return _psycopg_dsn(configured)
    configured = anncsu_dsn()
    if configured:
        return _psycopg_dsn(configured)
    url = make_url(sister_dsn)
    if (url.host or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
        return None
    return url.set(drivername="postgresql", database="anncsu").render_as_string(hide_password=False)


async def _load_references(connection: psycopg.AsyncConnection) -> list[dict[str, str]]:
    cursor = await connection.execute(_REFERENCE_QUERY)
    return [
        {
            "cadastral_code": str(row[0]).strip().upper(),
            "municipality_code": str(row[0]).strip().upper(),
            "section": str(row[1] or "").strip().upper(),
            "sheet": str(row[2]).strip(),
            "parcel": str(row[3]).strip(),
            "municipality": str(row[4] or "").strip(),
            "province": str(row[5] or "").strip(),
        }
        for row in await cursor.fetchall()
    ]


async def _load_existing(connection: psycopg.AsyncConnection) -> dict[tuple[str, str, str, str], str]:
    cursor = await connection.execute(
        "SELECT cadastral_code, section, sheet, parcel, location_source "
        "FROM sister.cadastral_parcel_coordinate"
    )
    rows = await cursor.fetchall()
    return {(str(a), str(b), str(c), str(d)): str(source) for a, b, c, d, source in rows}


def _parcel_key(item: dict[str, str]) -> tuple[str, str, str, str]:
    return item["cadastral_code"], item["section"], item["sheet"], item["parcel"]


async def _backfill_geometry(
    connection: psycopg.AsyncConnection,
    references: list[dict[str, str]],
    existing: dict[tuple[str, str, str, str], str],
    keys_per_query: int,
) -> int:
    from pyproj import Transformer
    from shapely.geometry import shape
    from shapely.ops import transform

    from sister.web import _read_subject_map_features_from_sister_views

    pending = [item for item in references if existing.get(_parcel_key(item)) != "cadastral_geometry"]
    saved = 0
    invalid = 0
    transformers: dict[int, Transformer] = {}
    for start in range(0, len(pending), keys_per_query):
        batch = pending[start : start + keys_per_query]
        rows, _ = await _read_subject_map_features_from_sister_views(
            connection, batch, [], max_keys_per_query=keys_per_query
        )
        inserts = []
        for row in rows:
            try:
                index = int(row[0])
                srid, max_srid = int(row[2]), int(row[3])
                geometry_value = row[6]
                if not 0 <= index < len(batch) or srid != max_srid or not geometry_value:
                    invalid += 1
                    continue
                if isinstance(geometry_value, str):
                    geometry_value = json.loads(geometry_value)
                geometry = shape(geometry_value)
                if srid != 4326:
                    transformer = transformers.get(srid)
                    if transformer is None:
                        transformer = Transformer.from_crs(srid, 4326, always_xy=True)
                        transformers[srid] = transformer
                    geometry = transform(transformer.transform, geometry)
                point = geometry.representative_point()
                longitude, latitude = float(point.x), float(point.y)
                if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
                    invalid += 1
                    continue
                item = batch[index]
                inserts.append((
                    *_parcel_key(item), latitude, longitude,
                    "cadastral_geometry", None, "point_on_surface",
                ))
            except Exception:  # noqa: BLE001 - one malformed geometry must not stop the resumable backfill
                invalid += 1
        if inserts:
            async with connection.cursor() as cursor:
                await cursor.executemany(_COORDINATE_UPSERT, inserts)
            await connection.commit()
            saved += len(inserts)
        processed = min(start + len(batch), len(pending))
        if processed == len(pending) or start // keys_per_query % 10 == 0:
            print(f"cadastral geometry: checked {processed}/{len(pending)}, saved {saved}, invalid {invalid}")
    return saved


async def _load_address_candidates(
    connection: psycopg.AsyncConnection,
    covered_codes: set[str],
) -> list[tuple[str, str, str, str, str, str, str, str | None]]:
    if not covered_codes:
        return []
    cursor = await connection.execute(_ADDRESS_QUERY)
    rows = await cursor.fetchall()
    return [row for row in rows if str(row[0]).upper() in covered_codes]


async def _backfill_anncsu(
    sister_connection: psycopg.AsyncConnection,
    anncsu_connection: psycopg.AsyncConnection,
) -> int:
    from sister.anncsu import parse_street_address

    cursor = await anncsu_connection.execute(_ANNCSU_COVERAGE_CODES)
    covered_codes = {str(row[0]).strip().upper() for row in await cursor.fetchall()}
    candidates = await _load_address_candidates(sister_connection, covered_codes)
    if not candidates:
        print("ANNCSU: no unresolved building addresses in covered municipalities")
        return 0

    wanted: dict[tuple[str, str, str], set[tuple[str, str, str, str]]] = defaultdict(set)
    for code, section, sheet, parcel, _municipality, _province, address, civic_number in candidates:
        address_text = str(address or "")
        parsed = parse_street_address(address_text)
        if not parsed and civic_number:
            parsed = parse_street_address(f"{address_text} {civic_number}")
        if not parsed:
            continue
        street, civic_numbers = parsed
        parcel_key = (str(code).upper(), str(section).upper(), str(sheet), str(parcel))
        for civic in civic_numbers:
            wanted[(parcel_key[0], street.upper(), str(civic))].add(parcel_key)

    if not wanted:
        print("ANNCSU: no exact street-and-civic candidates")
        return 0

    wanted_keys = sorted(wanted)
    codes, streets, civics = zip(*wanted_keys)
    anncsu_cursor = await anncsu_connection.execute(
        _ANNCSU_MATCH_QUERY, (list(codes), list(streets), list(civics))
    )
    addresses: dict[tuple[str, str, str], list[tuple[Any, ...]]] = defaultdict(list)
    for row in await anncsu_cursor.fetchall():
        key = (str(row[0]).upper(), str(row[1]).upper(), str(row[2]))
        addresses[key].append((row[3], float(row[4]), float(row[5]), row[6]))

    by_parcel: dict[tuple[str, str, str, str], list[tuple[Any, ...]]] = defaultdict(list)
    for key, parcel_keys in wanted.items():
        for parcel_key in parcel_keys:
            by_parcel[parcel_key].extend(addresses.get(key, []))

    inserts = []
    for parcel_key, matched_rows in by_parcel.items():
        if not matched_rows:
            continue
        matched_rows.sort(key=lambda row: (int(row[3]) if str(row[3]).isdigit() else 99, str(row[0])))
        best = matched_rows[0]
        best_point = (float(best[1]), float(best[2]))
        if any(_distance_m(best_point, (float(row[1]), float(row[2]))) > 100 for row in matched_rows):
            continue
        method = "coordinate_method_" + str(best[3] or "unknown")
        inserts.append((
            *parcel_key, best_point[1], best_point[0], str(best[0]), method,
        ))

    if inserts:
        async with sister_connection.cursor() as cursor:
            await cursor.executemany(_UPSERT_COORDINATE, inserts)
        await sister_connection.commit()
    print(f"ANNCSU: matched {len(inserts)} of {len(candidates)} unresolved address rows")
    return len(inserts)


async def run_backfill(keys_per_query: int, skip_anncsu: bool) -> None:
    from sister.database_config import get_database_dsn

    sister_dsn = get_database_dsn(required=True)
    schema = os.getenv("DATABASE_SCHEMA", "sister")
    if schema != "sister":
        raise RuntimeError("The cadastral source views currently require DATABASE_SCHEMA=sister")
    sister_connection = await _connection(_psycopg_dsn(sister_dsn), schema)
    anncsu_connection = None
    try:
        references = await _load_references(sister_connection)
        existing = await _load_existing(sister_connection)
        print(f"SISTER: {len(references)} parcel identities; {len(existing)} already located")
        geometry_saved = await _backfill_geometry(
            sister_connection, references, existing, keys_per_query
        )
        anncsu_saved = 0
        anncsu_dsn = None if skip_anncsu else _anncsu_dsn(sister_dsn)
        if anncsu_dsn:
            anncsu_connection = await _connection(anncsu_dsn, "anncsu")
            anncsu_saved = await _backfill_anncsu(sister_connection, anncsu_connection)
        elif not skip_anncsu:
            print("ANNCSU: skipped; set SISTER_ANNCSU_POSTGRES_DSN for this database")
        final = await _load_existing(sister_connection)
        located = len(final)
        unresolved = max(0, len(references) - located)
        print(
            f"complete: geometry_added={geometry_saved}, anncsu_added={anncsu_saved}, "
            f"located={located}/{len(references)}, unresolved={unresolved}"
        )
    finally:
        if anncsu_connection is not None:
            await anncsu_connection.close()
        await sister_connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keys-per-query", type=int, default=100,
        help="maximum parcel keys per cadastral lookup query (default: 100)",
    )
    parser.add_argument(
        "--skip-anncsu", action="store_true",
        help="backfill cadastral geometry only",
    )
    args = parser.parse_args()
    if not 1 <= args.keys_per_query <= 500:
        parser.error("--keys-per-query must be between 1 and 500")
    asyncio.run(run_backfill(args.keys_per_query, args.skip_anncsu))


if __name__ == "__main__":
    main()
