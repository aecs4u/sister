#!/usr/bin/env python3
"""Request missing analytical historical subject visure for Recrowd companies.

The default run is read-only and reports which Recrowd proponents have no matching document in the
local Sister PostgreSQL database. Pass ``--execute`` to submit missing requests through the running
Sister API. CAPTCHA completion remains a human step in the authenticated Sister browser session.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env", override=False)

RECROWD_DSN_DEFAULT = "postgresql+psycopg://postgres@localhost/recrowd"
SISTER_URL_DEFAULT = os.getenv("VISURA_API_URL", "http://localhost:8025")
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 300
RECENT_SUBMISSION_DAYS = 6
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

# raw payloads (response JSON, document text, XML tree) live in the archive schema
ARCHIVE = '"' + os.getenv("DATABASE_ARCHIVE_SCHEMA", "sister_archive") + '"'

HAS_ANALYTICAL_VISURA_SQL = text(f"""
    SELECT EXISTS (
        SELECT 1
        FROM document AS vd
        WHERE (
            LOWER(COALESCE(vd.document_type, '')) = 'visura_soggetto'
            OR (
                LOWER(COALESCE(vd.document_type, '')) = 'visura'
                AND (
                    LOWER(COALESCE(vd.subject_text, '')) LIKE '%soggetto%'
                    OR LOWER(COALESCE(vd.title, '')) LIKE '%soggetto%'
                )
            )
        )
          AND (
              LOWER(COALESCE(vd.view_subtype, '')) = 'storica_analitica'
              OR LOWER(COALESCE(vd.title, '')) LIKE '%storica analitica%'
              OR LOWER(COALESCE(vd.subject_text, '')) LIKE '%storica analitica%'
          )
          AND (
              EXISTS (
                  SELECT 1
                  FROM document_subject AS ds
                  JOIN subject AS cs ON cs.id = ds.subject_id
                  WHERE ds.document_id = vd.id AND cs.fiscal_code = :code
              )
              OR POSITION(:code IN UPPER(COALESCE(vd.file_name, ''))) > 0
              OR POSITION(:code IN UPPER(COALESCE(vd.subject_text, ''))) > 0
              OR EXISTS (
                  SELECT 1 FROM {ARCHIVE}.document_content AS dc
                  WHERE dc.document_id = vd.id AND POSITION(:code IN UPPER(dc.content)) > 0
              )
              OR EXISTS (
                  SELECT 1 FROM {ARCHIVE}.document_xml_node AS n
                  WHERE n.document_id = vd.id AND UPPER(BTRIM(COALESCE(n.text, ''))) = :code
              )
              OR EXISTS (
                  SELECT 1
                  FROM {ARCHIVE}.document_xml_node AS n
                  JOIN {ARCHIVE}.document_xml_attribute AS a ON a.node_id = n.id
                  WHERE n.document_id = vd.id AND UPPER(BTRIM(a.value)) = :code
              )
          )
    )
""")

HAS_RECENT_SUBMISSION_SQL = text(f"""
    SELECT EXISTS (
        SELECT 1
        FROM request AS vr
        JOIN {ARCHIVE}.request_payload AS payload ON payload.request_id = vr.id
        WHERE vr.request_type = 'soggetto_documento'
          AND vr.success IS TRUE
          AND vr.responded_at >= CURRENT_TIMESTAMP - (:days * INTERVAL '1 day')
          AND payload.data::jsonb ->> 'soggetto' = :code
          AND payload.data::jsonb ->> 'vista' = 'analitica'
          AND COALESCE(payload.data::jsonb ->> 'richieste', '[]') <> '[]'
    )
""")

HAS_RECENT_QUEUED_REQUEST_SQL = text("""
    SELECT EXISTS (
        SELECT 1
        FROM request AS vr
        WHERE vr.cache_key = :cache_key
          AND vr.responded_at IS NULL
          AND vr.created_at >= CURRENT_TIMESTAMP - (:minutes * INTERVAL '1 minute')
    )
""")


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


def _valid_company_code(value: str) -> bool:
    return bool(re.fullmatch(r"\d{11}|[A-Z0-9]{16}", value))


def _proponent_groups(engine, limit: int | None = None) -> tuple[int, list[dict], list[dict]]:
    with engine.connect() as conn:
        rows = conn.execute(RECROWD_PROPONENTS_SQL).mappings().all()

    groups: dict[str, dict] = {}
    missing: list[dict] = []
    invalid: list[dict] = []
    for row in rows:
        code = _normalize_code(row["fiscal_code"])
        base = {
            "proponent_id": str(row["proponent_id"]),
            "organization_name": row["organization_name"],
        }
        if not code:
            missing.append({**base, "status": "skipped_missing_code"})
            continue
        if not _valid_company_code(code):
            invalid.append({**base, "fiscal_code": code, "status": "skipped_invalid_code"})
            continue
        group = groups.setdefault(code, {"fiscal_code": code, "proponents": []})
        group["proponents"].append(base)

    ordered = [groups[code] for code in sorted(groups)]
    if limit is not None:
        ordered = ordered[:limit]
    return len(rows), ordered, missing + invalid


def _has_analytical_visura(engine, code: str) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(HAS_ANALYTICAL_VISURA_SQL, {"code": code}).scalar())


def _has_recent_submission(engine, code: str) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(
                HAS_RECENT_SUBMISSION_SQL,
                {"code": code, "days": RECENT_SUBMISSION_DAYS},
            ).scalar()
        )


def _request_cache_key(code: str) -> str:
    """Match VisuraService._request_cache_params for the generic legal-entity document request."""
    params = {
        "cadastre_type": "E",
        "province": "NAZIONALE",
        "search_type": "soggetto_documento",
        "codice_fiscale": code,
        "azienda": "true",
        "vista": "analitica",
    }
    canonical = json.dumps({"type": "soggetto_documento", **params}, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _has_recent_queued_request(engine, code: str) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(
                HAS_RECENT_QUEUED_REQUEST_SQL,
                {"cache_key": _request_cache_key(code), "minutes": RECENT_QUEUED_MINUTES},
            ).scalar()
        )


def _save_progress(path: Path, records: list[dict], *, download_result: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now().astimezone().isoformat(),
        "request": {
            "cadastre_type": "E",
            "view": "analitica",
            "subject_kind": "legal_entity",
        },
        "summary": {
            "total": len(records),
            "already_present": sum(r.get("status") == "already_present" for r in records),
            "missing": sum(r.get("status") == "missing_dry_run" for r in records),
            "submitted": sum(r.get("status") in {"completed", "cached"} for r in records),
            "errors": sum(r.get("status") in {"error", "failed", "timeout"} for r in records),
        },
        "download_result": download_result,
        "results": records,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _poll(client: httpx.Client, request_id: str, timeout: int, interval: int) -> dict:
    deadline = time.monotonic() + timeout
    terminal = {"completed", "cached", "error", "failed", "expired", "needs_human", "cancelled"}
    while time.monotonic() < deadline:
        response = client.get(f"/visura/{request_id}")
        response.raise_for_status()
        data = response.json()
        if data.get("status") in terminal:
            return data
        time.sleep(interval)
    return {"status": "timeout", "request_id": request_id}


def _submit(client: httpx.Client, code: str, timeout: int, interval: int) -> dict:
    response = client.post(
        "/visura/soggetto-documento",
        params={
            "provincia": "NAZIONALE",
            "tipo_catasto": "E",
            "codice_fiscale": code,
            "azienda": "true",
            "vista": "analitica",
        },
    )
    response.raise_for_status()
    submitted = response.json()
    request_id = submitted.get("request_id")
    if not request_id:
        return {"status": "error", "error": "Sister API did not return request_id", "submit": submitted}
    if submitted.get("status") == "cached":
        return {"status": "cached", "request_id": request_id, "submit": submitted}
    return {**_poll(client, request_id, timeout, interval), "submit": submitted}


def _download_ready_documents(client: httpx.Client) -> dict:
    """Download ready documents; Sister's endpoint downloads all ready Richieste items, not only Recrowd items."""
    response = client.post("/visura/download-documents")
    response.raise_for_status()
    return response.json()


def run(args: argparse.Namespace) -> int:
    sister_dsn = os.getenv("DATABASE_DSN")
    if not sister_dsn:
        raise RuntimeError("DATABASE_DSN must point to the local Sister PostgreSQL database")
    recrowd_dsn = args.recrowd_dsn or os.getenv("RECROWD_DATABASE_DSN") or RECROWD_DSN_DEFAULT

    recrowd_engine = create_engine(_postgres_url(recrowd_dsn), pool_pre_ping=True)
    sister_engine = create_engine(_postgres_url(sister_dsn), pool_pre_ping=True)
    output_path = args.output or (
        PROJECT_ROOT
        / "outputs"
        / f"recrowd_storica_analitica_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )

    try:
        total_proponents, groups, skipped = _proponent_groups(recrowd_engine, args.limit)
        print(
            f"Recrowd: {total_proponents} proponents; {len(groups)} unique usable tax codes; "
            f"{len(skipped)} proponents without a usable code."
        )
        records = list(skipped)
        for item in groups:
            code = item["fiscal_code"]
            names = [p["organization_name"] for p in item["proponents"]]
            if _has_analytical_visura(sister_engine, code):
                records.append({**item, "status": "already_present"})
                continue
            record = {**item, "status": "missing_dry_run"}
            if args.execute:
                if _has_recent_queued_request(sister_engine, code):
                    record["status"] = "request_in_progress"
                    record["note"] = "A matching request is already queued or processing; not submitting a duplicate."
                elif _has_recent_submission(sister_engine, code):
                    record["status"] = "awaiting_download"
                    record["note"] = "A recent analytical request exists; not submitting a duplicate."
                else:
                    try:
                        with httpx.Client(
                            base_url=args.sister_url.rstrip("/"),
                            headers=args.headers,
                            timeout=30,
                        ) as client:
                            result = _submit(client, code, args.timeout, args.poll_interval)
                        record.update(result)
                        if result.get("status") == "needs_human":
                            record["note"] = "CAPTCHA needs a human in the authenticated Sister browser."
                            records.append(record)
                            _save_progress(output_path, records)
                            print(f"CAPTCHA needed for {', '.join(names)}; saved progress and stopped the batch.")
                            return 2
                    except Exception as exc:  # Keep progress and continue with the other proponents.
                        record.update(status="error", error=str(exc))
                print(f"{', '.join(names)} ({code}): {record['status']}", flush=True)
            else:
                print(f"MISSING {', '.join(names)} ({code})", flush=True)
            records.append(record)
            _save_progress(output_path, records)

        download_result = None
        if args.download_ready:
            with httpx.Client(
                base_url=args.sister_url.rstrip("/"),
                headers=args.headers,
                timeout=30,
            ) as client:
                download_result = _download_ready_documents(client)
            for record in records:
                code = record.get("fiscal_code")
                if code and record.get("status") not in {"skipped_missing_code", "skipped_invalid_code"}:
                    if _has_analytical_visura(sister_engine, code):
                        record["status"] = "already_present" if record.get("status") == "already_present" else "downloaded"
            _save_progress(output_path, records, download_result=download_result)

        _save_progress(output_path, records, download_result=download_result)
        print(f"Saved batch report: {output_path}")
        if not args.execute:
            print("Dry run only. Pass --execute to submit the missing requests.")
        if args.execute and not args.download_ready:
            print("Requests are submitted; pass --download-ready to import all ready Sister documents into the database.")
        return 0
    finally:
        recrowd_engine.dispose()
        sister_engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Submit missing visura requests (default is read-only).")
    parser.add_argument(
        "--download-ready",
        action="store_true",
        help="Also download ready documents from Sister Richieste; this downloads all ready items, not only Recrowd.",
    )
    parser.add_argument("--limit", type=int, help="Process at most this many unique tax codes.")
    parser.add_argument("--recrowd-dsn", help="Override RECROWD_DATABASE_DSN (default: local postgres/recrowd).")
    parser.add_argument("--sister-url", default=SISTER_URL_DEFAULT, help="Sister API base URL.")
    parser.add_argument("--timeout", type=int, default=POLL_TIMEOUT_SECONDS, help="Max seconds to poll each request.")
    parser.add_argument("--poll-interval", type=int, default=POLL_INTERVAL_SECONDS, help="Seconds between polls.")
    parser.add_argument("--output", type=Path, help="Progress JSON path.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")
    if args.download_ready and not args.execute:
        parser.error("--download-ready requires --execute")
    api_key = os.getenv("VISURA_API_KEY") or os.getenv("API_KEY")
    args.headers = {"X-API-Key": api_key} if api_key else {}
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
