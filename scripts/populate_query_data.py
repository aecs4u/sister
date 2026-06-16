#!/usr/bin/env python3
"""Populate the SISTER SQLite database from exported query JSON files."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import sister.database as database
from sister.database import compute_cache_key, init_db
from sister.db_models import OWNER_RIGHT_FIELD_MAP, OWNER_SUBJECT_FIELD_MAP, PROPERTY_FIELD_MAP

REQUEST_TYPE_BY_PREFIX = {
    "req": "visura",
    "intestati": "intestati",
    "soggetto": "soggetto",
    "pnf": "persona_giuridica",
    "eimm": "elenco_immobili",
    "richieste": "richieste",
}

PROVINCE_BY_COMUNE = {
    "AGRIGENTO": "Agrigento",
    "PALERMO": "Palermo",
    "RAVENNA": "Ravenna",
    "ROMA": "Roma",
}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_response_payload(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("request_id"), str)
        and "data" in value
        and ("success" in value or "status" in value)
    )


def _iter_response_payloads(source: Path) -> Iterable[tuple[Path, dict[str, Any]]]:
    for path in sorted(source.glob("*.json")):
        payload = _load_json(path)
        if _is_response_payload(payload):
            yield path, payload
            continue
        if isinstance(payload, dict):
            for value in payload.values():
                if _is_response_payload(value):
                    yield path, value


def _request_type(request_id: str) -> str:
    parts = request_id.split("_")
    if len(parts) >= 2 and parts[0] == "wf":
        return f"workflow_{parts[1]}"
    return REQUEST_TYPE_BY_PREFIX.get(parts[0], parts[0] or "query")


def _created_at(path: Path, payload: dict[str, Any]) -> str:
    """Use current time so imported records aren't immediately expired by the cleanup task."""
    return datetime.now().isoformat()


def _success(payload: dict[str, Any]) -> bool:
    if "success" in payload:
        return bool(payload["success"])
    return payload.get("status") == "completed"


def _form_elements(data: dict[str, Any]) -> Iterable[dict[str, Any]]:
    visits = data.get("page_visits", [])
    if not isinstance(visits, list):
        return
    for visit in visits:
        if not isinstance(visit, dict):
            continue
        elements = visit.get("form_elements", [])
        if not isinstance(elements, list):
            continue
        for element in elements:
            if isinstance(element, dict):
                yield element


def _last_form_value(data: dict[str, Any], *names: str, skip_province_wide: bool = False) -> str:
    value = ""
    for element in _form_elements(data):
        if element.get("name") not in names:
            continue
        raw = str(element.get("value") or "").strip()
        if not raw:
            continue
        if skip_province_wide and raw.upper().startswith("TUTTA LA PROVINCIA"):
            continue
        value = raw
    return value


def _clean_comune(value: str) -> str:
    if not value or value.upper().startswith("TUTTA LA PROVINCIA"):
        return ""
    return value.split("(", 1)[0].strip()


def _split_foglio(raw: Any) -> tuple[str, str | None]:
    value = str(raw or "").strip()
    if "/" not in value:
        return value, None
    prefix, foglio = value.split("/", 1)
    return foglio.strip(), prefix.strip() or None


def _first_mapping(*items: Any) -> dict[str, Any]:
    for item in items:
        if isinstance(item, dict):
            return item
    return {}


def _first_immobile(data: dict[str, Any]) -> dict[str, Any]:
    immobili = data.get("immobili")
    if isinstance(immobili, list):
        for item in immobili:
            if isinstance(item, dict) and item:
                return item
    return _first_mapping(data.get("immobile"))


def _infer_from_request_id(request_id: str) -> dict[str, str]:
    parts = request_id.split("_")
    if len(parts) >= 6 and parts[0] == "wf":
        return {
            "provincia": parts[2],
            "comune": parts[2].upper(),
            "foglio": parts[3],
            "particella": parts[4],
        }
    return {}


def _infer_request_fields(payload: dict[str, Any]) -> dict[str, Any]:
    request_id = payload["request_id"]
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    inferred = _infer_from_request_id(request_id)

    comune = _clean_comune(_last_form_value(data, "denomComune", "comuneCat", skip_province_wide=True))
    first = _first_immobile(data)
    foglio, sezione_from_foglio = _split_foglio(first.get("Foglio"))

    fields = {
        "request_id": request_id,
        "request_type": _request_type(request_id),
        "tipo_catasto": payload.get("tipo_catasto") or "",
        "provincia": data.get("provincia") or inferred.get("provincia") or "",
        "comune": data.get("comune") or comune or inferred.get("comune") or "",
        "foglio": data.get("foglio") or _last_form_value(data, "foglio") or inferred.get("foglio") or foglio,
        "particella": (
            data.get("particella")
            or _last_form_value(data, "particella1")
            or inferred.get("particella")
            or first.get("Particella")
            or data.get("soggetto")
            or ""
        ),
        "sezione": _last_form_value(data, "sezUrb", "sezione") or sezione_from_foglio,
        "subalterno": _last_form_value(data, "subalterno1") or first.get("Sub"),
    }

    if not fields["provincia"] and fields["comune"]:
        fields["provincia"] = PROVINCE_BY_COMUNE.get(str(fields["comune"]).upper(), "")

    cache_params = {
        "tipo_catasto": fields["tipo_catasto"],
        "provincia": fields["provincia"],
        "comune": fields["comune"],
        "foglio": fields["foglio"],
        "particella": fields["particella"],
        "sezione": fields["sezione"],
        "subalterno": fields["subalterno"],
    }
    if fields["request_type"] in {"soggetto", "persona_giuridica"} and data.get("soggetto"):
        key = "codice_fiscale" if fields["request_type"] == "soggetto" else "identificativo"
        cache_params[key] = data["soggetto"]
    fields["cache_key"] = compute_cache_key(fields["request_type"], **cache_params)
    return fields


def _parse_immobili(
    response_id: str, tipo_catasto: str, data: dict[str, Any]
) -> list[tuple[dict[str, Any], dict[str, str]]]:
    """Returns (property_fields, location_fields) pairs."""
    rows = []
    immobili = data.get("immobili", [])
    if not isinstance(immobili, list):
        return rows
    _CATASTO_TYPE = {"F": "building", "T": "land", "E": "entity"}
    from sister.db_models import PROPERTY_LOCATION_FIELD_MAP
    for item in immobili:
        if not isinstance(item, dict):
            continue
        prop: dict[str, Any] = {
            "response_id": response_id,
            "property_type": _CATASTO_TYPE.get(tipo_catasto),
        }
        loc: dict[str, str] = {
            "cadastre_type": tipo_catasto,
            "province": "",
            "municipality": "",
            "sheet": "",
            "parcel": "",
            "subunit": "",
            "section": "",
        }
        for html_key, db_col in PROPERTY_FIELD_MAP.items():
            if html_key in item:
                value = str(item[html_key]).strip()
                prop[db_col] = value or None
        for html_key, loc_col in PROPERTY_LOCATION_FIELD_MAP.items():
            if html_key in item:
                loc[loc_col] = str(item[html_key]).strip()
        rows.append((prop, loc))
    return rows


def _parse_intestati(response_id: str, data: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Parse intestati from response JSON into (subject_fields, right_fields) pairs."""
    rows = []
    intestati = data.get("intestati", [])
    if not isinstance(intestati, list):
        return rows
    for item in intestati:
        if not isinstance(item, dict):
            continue
        subject_fields: dict[str, Any] = {}
        right_fields: dict[str, Any] = {}
        for html_key, db_col in OWNER_SUBJECT_FIELD_MAP.items():
            if html_key in item:
                subject_fields[db_col] = str(item[html_key]).strip() or None
        for html_key, db_col in OWNER_RIGHT_FIELD_MAP.items():
            if html_key in item:
                right_fields[db_col] = str(item[html_key]).strip() or None
        rows.append((subject_fields, right_fields))
    return rows


def _parse_timestamp(raw: Any) -> str | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).isoformat()
    except ValueError:
        return None


def _parse_page_visits(response_id: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    visits = data.get("page_visits", [])
    if not isinstance(visits, list):
        return []
    rows = []
    for item in visits:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "response_id": response_id,
                "step": item.get("step", ""),
                "url": item.get("url"),
                "screenshot_url": item.get("screenshot_url"),
                "form_elements_json": (
                    json.dumps(item.get("form_elements", []), ensure_ascii=False, default=str)
                    if item.get("form_elements")
                    else None
                ),
                "errors_json": (
                    json.dumps(item.get("errors", []), ensure_ascii=False, default=str) if item.get("errors") else None
                ),
                "timestamp": _parse_timestamp(item.get("timestamp")),
            }
        )
    return rows


def _insert_mapping(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        [row[column] for column in columns],
    )


def _get_or_create_subject_sql(conn: sqlite3.Connection, **kwargs: Any) -> int | None:
    """Get existing CadastralSubject by fiscal_code (when present) or create one; return its id."""
    fiscal_code = kwargs.get("fiscal_code")
    if fiscal_code:
        row = conn.execute("SELECT id FROM cadastral_subjects WHERE fiscal_code=?", (fiscal_code,)).fetchone()
        if row:
            return row[0]
    columns = list(kwargs)
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO cadastral_subjects ({', '.join(columns)}) VALUES ({placeholders})",
        [kwargs[c] for c in columns],
    )
    return cursor.lastrowid


def _get_or_create_right_sql(conn: sqlite3.Connection, **kwargs: Any) -> int | None:
    """Get existing OwnershipRight or create one; return its id."""
    fields = ["right_type", "right_code", "right_description", "ownership_share", "start_date", "end_date"]
    where_parts = []
    params = []
    for f in fields:
        val = kwargs.get(f)
        if val is None:
            where_parts.append(f"{f} IS NULL")
        else:
            where_parts.append(f"{f}=?")
            params.append(val)
    row = conn.execute(f"SELECT id FROM ownership_rights WHERE {' AND '.join(where_parts)}", params).fetchone()
    if row:
        return row[0]
    columns = [f for f in fields if kwargs.get(f) is not None]
    if not columns:
        return None
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO ownership_rights ({', '.join(columns)}) VALUES ({placeholders})",
        [kwargs[c] for c in columns],
    )
    return cursor.lastrowid


def _get_or_create_location_sql(conn: sqlite3.Connection, **kwargs: str) -> int | None:
    """Get existing or create new CadastralLocation row via raw sqlite3; return its id."""
    cadastre_type = kwargs.get("cadastre_type") or ""
    province = kwargs.get("province") or ""
    municipality = kwargs.get("municipality") or ""
    sheet = kwargs.get("sheet") or ""
    parcel = kwargs.get("parcel") or ""
    subunit = kwargs.get("subunit") or ""
    section = kwargs.get("section") or ""
    row = conn.execute(
        "SELECT id FROM cadastral_locations WHERE cadastre_type=? AND province=? AND municipality=?"
        " AND sheet=? AND parcel=? AND subunit=? AND section=?",
        (cadastre_type, province, municipality, sheet, parcel, subunit, section),
    ).fetchone()
    if row:
        return row[0]
    cursor = conn.execute(
        "INSERT INTO cadastral_locations (cadastre_type, province, municipality, sheet, parcel, subunit, section)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cadastre_type, province, municipality, sheet, parcel, subunit, section),
    )
    return cursor.lastrowid


def _upsert_response(conn: sqlite3.Connection, path: Path, payload: dict[str, Any]) -> None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    fields = _infer_request_fields(payload)
    request_id = fields["request_id"]
    created_at = _created_at(path, payload)

    conn.execute("DELETE FROM page_visits WHERE response_id = ?", (request_id,))
    conn.execute("DELETE FROM visura_owners WHERE response_id = ?", (request_id,))
    conn.execute("DELETE FROM visura_properties WHERE response_id = ?", (request_id,))
    conn.execute("DELETE FROM visura_responses WHERE request_id = ?", (request_id,))
    conn.execute("DELETE FROM visura_requests WHERE request_id = ?", (request_id,))

    location_id = _get_or_create_location_sql(
        conn,
        cadastre_type=fields["tipo_catasto"],
        province=fields["provincia"],
        municipality=fields["comune"],
        sheet=fields["foglio"],
        parcel=fields["particella"],
        subunit=fields["subalterno"] or "",
        section=fields["sezione"] or "",
    )
    conn.execute(
        "INSERT INTO visura_requests (request_id, request_type, location_id, cache_key, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (fields["request_id"], fields["request_type"], location_id, fields["cache_key"], created_at),
    )
    conn.execute(
        """
        INSERT INTO visura_responses (request_id, success, cadastre_type, data, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            1 if _success(payload) else 0,
            fields["tipo_catasto"],
            json.dumps(data, ensure_ascii=False, default=str),
            payload.get("error"),
            created_at,
        ),
    )

    for prop_fields, loc_fields in _parse_immobili(request_id, fields["tipo_catasto"], data):
        # Inherit request province/municipality for complete location
        if not loc_fields["province"]:
            loc_fields["province"] = fields["provincia"] or ""
        if not loc_fields["municipality"]:
            loc_fields["municipality"] = fields["comune"] or ""
        prop_location_id = _get_or_create_location_sql(conn, **loc_fields)
        prop_fields["location_id"] = prop_location_id
        _insert_mapping(conn, "visura_properties", prop_fields)
    for subject_fields, right_fields in _parse_intestati(request_id, data):
        subject_id = _get_or_create_subject_sql(conn, **subject_fields) if subject_fields else None
        right_id = _get_or_create_right_sql(conn, **right_fields) if right_fields else None
        _insert_mapping(conn, "visura_owners", {"response_id": request_id, "subject_id": subject_id, "right_id": right_id})
    for row in _parse_page_visits(request_id, data):
        _insert_mapping(conn, "page_visits", row)


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _tables_exist(db_path: Path) -> bool:
    """Check whether the core sister tables already exist."""
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    return "visura_requests" in tables and "visura_responses" in tables


def _load_manifest(path: Path) -> dict[str, dict]:
    """Load the processed-file manifest; return empty dict if absent or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def _save_manifest(path: Path, manifest: dict[str, dict]) -> None:
    """Persist the manifest atomically via a temp-file rename."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def _manifest_key(path: Path) -> str:
    return str(path.resolve())


def _is_known(path: Path, manifest: dict) -> bool:
    entry = manifest.get(_manifest_key(path))
    if not entry:
        return False
    stat = path.stat()
    return entry.get("mtime") == stat.st_mtime and entry.get("size") == stat.st_size


def populate(
    db_path: Path,
    source: Path,
    dry_run: bool,
    manifest_path: Path | None = None,
) -> dict[str, int]:
    manifest = _load_manifest(manifest_path) if manifest_path else {}

    all_payloads = list(_iter_response_payloads(source))
    new_payloads = [(path, payload) for path, payload in all_payloads if not _is_known(path, manifest)]

    if dry_run:
        return {
            "files_total": len({path for path, _ in all_payloads}),
            "files_new": len({path for path, _ in new_payloads}),
            "responses_new": len(new_payloads),
        }

    if not _tables_exist(db_path):
        database.DB_PATH = str(db_path)
        database._engine = None
        asyncio.run(init_db())

    processed: set[Path] = set()
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        for path, payload in new_payloads:
            _upsert_response(conn, path, payload)
            processed.add(path)
            if manifest_path:
                stat = path.stat()
                manifest[_manifest_key(path)] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "processed_at": datetime.now().isoformat(),
                }
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    if manifest_path and processed:
        _save_manifest(manifest_path, manifest)

    with sqlite3.connect(db_path) as conn:
        return {
            "files_new": len(processed),
            "files_skipped": len({p for p, _ in all_payloads}) - len(processed),
            "visura_requests": _count(conn, "visura_requests"),
            "visura_responses": _count(conn, "visura_responses"),
            "visura_properties": _count(conn, "visura_properties"),
            "visura_owners": _count(conn, "visura_owners"),
            "page_visits": _count(conn, "page_visits"),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/sister.sqlite"), help="SQLite database path")
    parser.add_argument("--source", type=Path, default=Path("outputs"), help="Directory containing exported JSON files")
    parser.add_argument("--manifest", type=Path, default=None, help="JSON file tracking processed files (skips re-processing)")
    parser.add_argument("--dry-run", action="store_true", help="Scan files without writing to the database")
    args = parser.parse_args()

    stats = populate(args.db, args.source, args.dry_run, manifest_path=args.manifest)
    for key, value in stats.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
