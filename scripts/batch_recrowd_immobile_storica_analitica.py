#!/usr/bin/env python3
"""Find Recrowd-owned properties and request missing historical analytical visure per immobile.

The default run lists the Recrowd proponents' properties through Sister and compares them with the
local Sister PostgreSQL document index. It does not request visure. Pass ``--execute`` to submit a
Storica Analitica per Immobile request for each property without a matching stored document.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env", override=False)

from sister.database import _property_row  # noqa: E402 — reuse Sister's canonical property/location parser

RECROWD_DSN_DEFAULT = "postgresql+psycopg://postgres@localhost/recrowd"
SISTER_URL_DEFAULT = os.getenv("VISURA_API_URL") or os.getenv("SISTER_URL", "http://localhost:8025")
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 900
RECENT_REQUEST_DAYS = 6
RECENT_QUEUED_MINUTES = 60

RECROWD_PROPONENTS_SQL = text("""
    SELECT p.id AS proponent_id,
           p.organization_name,
           COALESCE(
               NULLIF(BTRIM(d.tax_code), ''),
               NULLIF(BTRIM(d.vat_code), ''),
               NULLIF(BTRIM(p.vat_number), '')
           ) AS fiscal_code
    FROM recrowd_proponents AS p
    LEFT JOIN recrowd_proponent_company_details AS d ON d.proponent_id = p.id
    ORDER BY p.organization_name
""")

# raw response JSON lives in the archive schema
ARCHIVE = '"' + os.getenv("DATABASE_ARCHIVE_SCHEMA", "sister_archive") + '"'

HAS_PROPERTY_VISURA_SQL = text("""
    SELECT EXISTS (
        SELECT 1
        FROM document AS vd
        JOIN cadastral_unit AS loc ON loc.id = vd.cadastral_unit_id
        JOIN comune AS com ON com.id = loc.comune_id
        WHERE COALESCE(loc.cadastre_type, '') = :cadastre_type
          AND com.province_code IN (:province, :province_code)
          AND com.name = :municipality
          AND BTRIM(COALESCE(loc.sheet, '')) = :sheet
          AND BTRIM(COALESCE(loc.parcel, '')) = :parcel
          AND UPPER(BTRIM(COALESCE(loc.subunit, ''))) = :subunit
          AND UPPER(BTRIM(COALESCE(loc.section, ''))) = :section
          AND (
              REPLACE(REPLACE(LOWER(COALESCE(vd.view_subtype, '')), '_', ' '), '-', ' ')
                  = 'storica analitica'
              OR REPLACE(REPLACE(LOWER(COALESCE(vd.title, '')), '_', ' '), '-', ' ')
                  LIKE '%storica analitica%'
              OR REPLACE(REPLACE(LOWER(COALESCE(vd.subject_text, '')), '_', ' '), '-', ' ')
                  LIKE '%storica analitica%'
              OR REPLACE(REPLACE(LOWER(COALESCE(vd.file_name, '')), '_', ' '), '-', ' ')
                  LIKE '%storica analitica%'
          )
    )
""")

RECENT_REQUEST_SQL = text(f"""
    SELECT CASE
        WHEN vr.responded_at IS NULL THEN 'queued'
        WHEN COALESCE(payload.data::jsonb, '{{}}'::jsonb) ? 'needs_human' THEN 'needs_human'
        ELSE 'recent_response'
    END
    FROM request AS vr
    LEFT JOIN {ARCHIVE}.request_payload AS payload ON payload.request_id = vr.id
    WHERE vr.cache_key = :cache_key
      AND vr.request_type = 'visura_storica'
      AND vr.created_at >= CURRENT_TIMESTAMP - (:days * INTERVAL '1 day')
      AND (vr.responded_at IS NULL OR vr.success IS TRUE)
    ORDER BY vr.created_at DESC
    LIMIT 1
""")

RECENT_QUEUED_SQL = text("""
    SELECT EXISTS (
        SELECT 1
        FROM request AS vr
        WHERE vr.cache_key = :cache_key
          AND vr.request_type = 'visura_storica'
          AND vr.responded_at IS NULL
          AND vr.created_at >= CURRENT_TIMESTAMP - (:minutes * INTERVAL '1 minute')
    )
""")


class SisterSessionExpired(RuntimeError):
    """Sister's authenticated SISTER portal session needs attention."""


def _postgres_url(raw: str) -> URL:
    url = make_url(raw)
    if url.get_backend_name() != "postgresql":
        raise ValueError("Database DSNs must use PostgreSQL")
    if url.drivername in {
        "postgres",
        "postgresql",
        "postgresql+asyncpg",
        "postgresql+psycopg2",
        "postgresql+psycopg_async",
    }:
        url = url.set(drivername="postgresql+psycopg")
    return url


def _normalize_code(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _valid_tax_code(value: str) -> bool:
    return bool(re.fullmatch(r"\d{11}|[A-Z0-9]{16}", value))


def _proponent_groups(engine, limit: int | None = None) -> tuple[int, list[dict], list[dict]]:
    with engine.connect() as conn:
        rows = conn.execute(RECROWD_PROPONENTS_SQL).mappings().all()

    groups: dict[str, dict] = {}
    skipped: list[dict] = []
    for row in rows:
        code = _normalize_code(row["fiscal_code"])
        proponent = {
            "proponent_id": str(row["proponent_id"]),
            "organization_name": row["organization_name"],
        }
        if not code:
            skipped.append({**proponent, "status": "skipped_missing_code"})
        elif not _valid_tax_code(code):
            skipped.append({**proponent, "fiscal_code": code, "status": "skipped_invalid_code"})
        else:
            groups.setdefault(code, {"fiscal_code": code, "proponents": []})["proponents"].append(proponent)

    ordered = [groups[code] for code in sorted(groups)]
    if limit is not None:
        ordered = ordered[:limit]
    return len(rows), ordered, skipped


def _headers() -> dict[str, str]:
    api_key = os.getenv("VISURA_API_KEY") or os.getenv("API_KEY")
    return {"X-API-Key": api_key} if api_key else {}


def _is_expired(payload: dict) -> bool:
    error = str(payload.get("error") or (payload.get("data") or {}).get("error") or "").lower()
    return any(term in error for term in ("sessione scaduta", "sessione non valida", "nessuna provincia disponibile"))


def _poll(client: httpx.Client, request_id: str, timeout: int, interval: int) -> dict:
    deadline = time.monotonic() + timeout
    terminal = {"completed", "cached", "error", "failed", "expired", "needs_human", "cancelled", "not_found"}
    while time.monotonic() < deadline:
        response = client.get(f"/visura/{request_id}")
        if response.status_code == 404:
            return {"status": "not_found", "request_id": request_id}
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") in terminal:
            if _is_expired(payload):
                raise SisterSessionExpired(str(payload.get("error") or "sessione SISTER scaduta"))
            return payload
        time.sleep(interval)
    return {"status": "timeout", "request_id": request_id}


def _fetch_properties(client: httpx.Client, tax_code: str, timeout: int, interval: int) -> tuple[list[dict] | None, str]:
    params = {
        "provincia": "NAZIONALE",
        "codice_fiscale": tax_code,
        "tipo_catasto": "E",
        "con_intestati": "false",
    }
    if len(tax_code) == 11 and tax_code.isdigit():
        params["azienda"] = "true"
    response = client.post("/visura/soggetto-immobili", params=params)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") not in {"completed", "cached", "error", "failed", "needs_human"}:
        request_id = payload.get("request_id")
        if not request_id:
            return None, "Sister API did not return request_id"
        payload = _poll(client, request_id, timeout, interval)
    if _is_expired(payload):
        raise SisterSessionExpired(str(payload.get("error") or "sessione SISTER scaduta"))
    if payload.get("status") in {"needs_human", "error", "failed", "not_found", "timeout"}:
        return None, str(payload.get("error") or payload.get("status"))
    data = payload.get("data") or {}
    if data.get("error"):
        if "scadut" in str(data["error"]).lower() or "sessione" in str(data["error"]).lower():
            raise SisterSessionExpired(str(data["error"]))
        return [], str(data["error"])
    return data.get("immobili") or [], ""


def _location_from_row(row: dict) -> dict[str, str] | None:
    """Use the same decoding as Sister's response persistence for exact DB matching and API parameters."""
    _property, location, _subject = _property_row("batch", "E", row)
    location = {key: str(value or "").strip() for key, value in location.items()}
    if location.get("cadastre_type") not in {"F", "T"}:
        return None
    required = ("province", "municipality", "sheet", "parcel")
    if any(not location.get(key) for key in required):
        return None
    return location


def _location_key(location: dict[str, str]) -> str:
    fields = (
        "cadastre_type",
        "province",
        "municipality",
        "sheet",
        "parcel",
        "subunit",
        "section",
    )
    return "|".join(location.get(field, "").strip().upper() for field in fields)


def _request_cache_key(location: dict[str, str]) -> str:
    """Mirror VisuraService._request_cache_params and compute_cache_key for visura_storica."""
    params: dict[str, str] = {
        "cadastre_type": location["cadastre_type"],
        "province": location["province"],
        "municipality": location["municipality"],
        "search_type": "visura_storica",
        "foglio": location["sheet"],
        "particella": location["parcel"],
    }
    if location.get("section"):
        params["sezione"] = location["section"]
    if location.get("subunit"):
        params["subalterno"] = location["subunit"]
    canonical = json.dumps({"type": "visura_storica", **params}, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _has_property_visura(engine, location: dict[str, str], province_code: str = "") -> bool:
    from sister.provinces import province_code as to_sigla
    from sister.reference import place_name

    params = {
        **location,
        # the database stores the sigla: accept the province as a name or a sigla, in either argument
        "province": to_sigla(location["province"]) or "",
        "province_code": to_sigla(province_code) or to_sigla(location["province"]) or "",
        "municipality": place_name(location["municipality"]) or "",
        "sheet": location["sheet"].strip(),
        "parcel": location["parcel"].strip(),
        "subunit": location.get("subunit", "").strip().upper(),
        "section": location.get("section", "").strip().upper(),
    }
    with engine.connect() as conn:
        return bool(conn.execute(HAS_PROPERTY_VISURA_SQL, params).scalar())


def _recent_request(engine, location: dict[str, str]) -> str | None:
    cache_key = _request_cache_key(location)
    with engine.connect() as conn:
        queued = conn.execute(
            RECENT_QUEUED_SQL,
            {"cache_key": cache_key, "minutes": RECENT_QUEUED_MINUTES},
        ).scalar()
        if queued:
            return "queued"
        return conn.execute(
            RECENT_REQUEST_SQL,
            {"cache_key": cache_key, "days": RECENT_REQUEST_DAYS},
        ).scalar()


def _property_request_params(location: dict[str, str]) -> dict[str, str]:
    params = {
        "provincia": location["province"],
        "comune": location["municipality"],
        "tipo_catasto": location["cadastre_type"],
        "foglio": location["sheet"],
        "particella": location["parcel"],
    }
    if location.get("section"):
        params["sezione"] = location["section"]
    if location.get("subunit"):
        params["subalterno"] = location["subunit"]
    return params


def _save_report(path: Path, proponents: list[dict], properties: dict[str, dict], skipped: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    property_rows = list(properties.values())
    counts: dict[str, int] = {}
    for item in skipped + property_rows:
        status = item.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    report = {
        "created_at": datetime.now().astimezone().isoformat(),
        "request": {"search_type": "visura_storica", "document": "storica_analitica", "scope": "per_immobile"},
        "summary": {
            "proponents_queried": len(proponents),
            "unique_properties": len(property_rows),
            "by_status": counts,
        },
        "proponents": proponents,
        "properties": sorted(property_rows, key=lambda item: item["property_key"]),
        "skipped": skipped,
    }
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(path)


def run(args: argparse.Namespace) -> int:
    sister_dsn = os.getenv("DATABASE_DSN")
    if not sister_dsn:
        raise RuntimeError("DATABASE_DSN must point to the local Sister PostgreSQL database")
    recrowd_dsn = args.recrowd_dsn or os.getenv("RECROWD_DATABASE_DSN") or RECROWD_DSN_DEFAULT
    recrowd_engine = create_engine(_postgres_url(recrowd_dsn), pool_pre_ping=True)
    sister_engine = create_engine(_postgres_url(sister_dsn), pool_pre_ping=True)
    output_path = args.output or (
        PROJECT_ROOT / "outputs" / f"recrowd_immobile_storica_analitica_{datetime.now():%Y%m%d_%H%M%S}.json"
    )
    proponents: list[dict] = []
    properties: dict[str, dict] = {}
    skipped: list[dict] = []

    try:
        total_proponents, groups, invalid = _proponent_groups(recrowd_engine, args.limit_proponents)
        skipped.extend(invalid)
        print(
            f"Recrowd: {total_proponents} proponents; {len(groups)} unique usable tax codes; "
            f"{len(invalid)} proponents without a usable code."
        )
        with httpx.Client(
            base_url=args.sister_url.rstrip("/"),
            headers=_headers(),
            timeout=60,
        ) as client:
            for index, group in enumerate(groups, start=1):
                code = group["fiscal_code"]
                names = [p["organization_name"] for p in group["proponents"]]
                proponent_record = {**group, "status": "properties_loaded", "property_count": 0}
                try:
                    rows, error = _fetch_properties(client, code, args.timeout, args.poll_interval)
                except SisterSessionExpired as exc:
                    proponent_record.update(status="session_expired", error=str(exc))
                    proponents.append(proponent_record)
                    _save_report(output_path, proponents, properties, skipped)
                    print(f"SISTER session expired while listing {', '.join(names)}; saved progress to {output_path}.")
                    return 3
                except Exception as exc:
                    proponent_record.update(status="error", error=str(exc))
                    proponents.append(proponent_record)
                    _save_report(output_path, proponents, properties, skipped)
                    print(f"ERROR listing {', '.join(names)}: {exc}", flush=True)
                    continue

                if rows is None:
                    proponent_record.update(status="listing_error", error=error)
                elif error:
                    proponent_record["note"] = error
                proponent_record["property_count"] = len(rows or [])
                proponents.append(proponent_record)

                for row in rows or []:
                    if not isinstance(row, dict):
                        skipped.append({
                            "status": "skipped_unparseable_property",
                            "fiscal_code": code,
                            "proponents": names,
                            "listing_row": row,
                        })
                        continue
                    location = _location_from_row(row)
                    if location is None:
                        skipped.append({
                            "status": "skipped_unparseable_property",
                            "fiscal_code": code,
                            "proponents": names,
                            "listing_row": row,
                        })
                        continue
                    key = _location_key(location)
                    property_record = properties.setdefault(
                        key,
                        {
                            "property_key": key,
                            "location": location,
                            "province_code": str(row.get("provincia") or "").strip(),
                            "source_row": row,
                            "owned_by": [],
                        },
                    )
                    if not any(owner.get("fiscal_code") == code for owner in property_record["owned_by"]):
                        property_record["owned_by"].append({"fiscal_code": code, "organization_names": names})

                _save_report(output_path, proponents, properties, skipped)
                print(f"[{index}/{len(groups)}] {', '.join(names)}: {len(rows or [])} properties", flush=True)

                if args.limit_properties and len(properties) >= args.limit_properties:
                    break

        if args.limit_properties:
            properties = dict(list(properties.items())[: args.limit_properties])

        print(f"Unique listed properties: {len(properties)}")
        for index, item in enumerate(properties.values(), start=1):
            location = item["location"]
            if _has_property_visura(sister_engine, location, item.get("province_code", "")):
                item["status"] = "already_present"
            elif not args.execute:
                item["status"] = "missing_dry_run"
            else:
                recent = _recent_request(sister_engine, location)
                if recent == "queued":
                    item["status"] = "request_in_progress"
                elif recent == "recent_response":
                    item["status"] = "awaiting_document"
                else:
                    try:
                        with httpx.Client(
                            base_url=args.sister_url.rstrip("/"),
                            headers=_headers(),
                            timeout=60,
                        ) as request_client:
                            response = request_client.post(
                                "/visura/visura-storica",
                                params=_property_request_params(location),
                            )
                            response.raise_for_status()
                            submit = response.json()
                            request_id = submit.get("request_id")
                            if submit.get("status") == "cached":
                                result = submit
                            elif not request_id:
                                result = {"status": "error", "error": "Sister API did not return request_id"}
                            else:
                                result = _poll(request_client, request_id, args.timeout, args.poll_interval)
                        item.update({"request_id": request_id, "result": result})
                        data = result.get("data") or {}
                        if result.get("status") == "needs_human" or data.get("needs_human"):
                            item["status"] = "needs_human"
                            item["note"] = "Solve the CAPTCHA in Sister's authenticated browser, then rerun the batch."
                        elif result.get("status") in {"completed", "cached"}:
                            item["status"] = "completed" if result.get("status") == "completed" else "cached_response"
                        else:
                            item["status"] = result.get("status", "error")
                    except SisterSessionExpired as exc:
                        item.update(status="session_expired", error=str(exc))
                    except Exception as exc:
                        item.update(status="error", error=str(exc))

                if item["status"] == "needs_human":
                    _save_report(output_path, proponents, properties, skipped)
                    print(f"CAPTCHA needed for {item['property_key']}; saved progress and stopped the batch.")
                    return 2
                if item["status"] == "session_expired":
                    _save_report(output_path, proponents, properties, skipped)
                    print(f"SISTER session expired; saved progress to {output_path}.")
                    return 3

            _save_report(output_path, proponents, properties, skipped)
            print(f"[{index}/{len(properties)}] {item['property_key']}: {item['status']}", flush=True)

        _save_report(output_path, proponents, properties, skipped)
        print(f"Saved batch report: {output_path}")
        if not args.execute:
            print("Preview only: properties were listed and checked, but no historical visure were requested.")
            print("Pass --execute to submit requests for missing documents.")
        return 0
    except KeyboardInterrupt:
        _save_report(output_path, proponents, properties, skipped)
        print(f"Interrupted; saved progress to {output_path}.")
        return 130
    finally:
        recrowd_engine.dispose()
        sister_engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Submit missing historical per-immobile visure.")
    parser.add_argument("--limit-proponents", type=int, help="List holdings for at most this many unique tax codes.")
    parser.add_argument("--limit-properties", type=int, help="Process at most this many unique properties.")
    parser.add_argument("--recrowd-dsn", help="Override RECROWD_DATABASE_DSN (default: local postgres/recrowd).")
    parser.add_argument("--sister-url", default=SISTER_URL_DEFAULT, help="Sister API base URL.")
    parser.add_argument("--timeout", type=int, default=POLL_TIMEOUT_SECONDS, help="Max seconds to poll each request.")
    parser.add_argument("--poll-interval", type=int, default=POLL_INTERVAL_SECONDS, help="Seconds between polls.")
    parser.add_argument("--output", type=Path, help="Progress JSON path.")
    args = parser.parse_args()
    for option in ("limit_proponents", "limit_properties"):
        if getattr(args, option) is not None and getattr(args, option) < 1:
            parser.error(f"--{option.replace('_', '-')} must be a positive integer")
    args.poll_interval = max(args.poll_interval, 1)
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
