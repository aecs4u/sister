#!/usr/bin/env python3
"""Query SISTER /visura/soggetto for all Recrowd proponents fiscal codes."""

import json
import os
import time
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env", override=False)

SISTER_URL = "http://localhost:8025"
OUTPUT_PATH = (
    PROJECT_ROOT / "outputs" / f"recrowd_soggetto_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
)
POLL_INTERVAL = 5  # seconds between status checks
TIMEOUT = 300  # max seconds to wait per query


def get_proponents() -> list[dict]:
    database_dsn = os.getenv("RECROWD_DATABASE_DSN")
    if not database_dsn:
        raise RuntimeError("RECROWD_DATABASE_DSN must point to the Recrowd PostgreSQL database")
    url = make_url(database_dsn)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("RECROWD_DATABASE_DSN must use PostgreSQL")
    if url.drivername in {
        "postgres",
        "postgresql",
        "postgresql+asyncpg",
        "postgresql+psycopg2",
        "postgresql+psycopg_async",
    }:
        url = url.set(drivername="postgresql+psycopg")

    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT p.organization_name,
                           COALESCE(d.vat_code, p.vat_number) AS vat_number
                    FROM recrowd_proponents p
                    LEFT JOIN recrowd_proponent_company_details d ON d.proponent_id = p.id
                    WHERE COALESCE(d.vat_code, p.vat_number) IS NOT NULL
                      AND COALESCE(d.vat_code, p.vat_number) != ''
                    ORDER BY p.organization_name
                """)
            )
            return [dict(row) for row in result.mappings()]
    finally:
        engine.dispose()


def submit(client: httpx.Client, vat_number: str) -> str | None:
    resp = client.post(
        f"{SISTER_URL}/visura/persona-giuridica",
        json={"identificativo": vat_number, "tipo_catasto": "E"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("request_id")


def poll(client: httpx.Client, request_id: str) -> dict:
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        resp = client.get(f"{SISTER_URL}/visura/{request_id}", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        if status in ("completed", "error", "failed"):
            return data
        time.sleep(POLL_INTERVAL)
    return {"status": "timeout", "request_id": request_id}


def main():
    proponents = get_proponents()
    total = len(proponents)
    print(f"Found {total} proponents with VAT numbers", flush=True)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    results = []

    with httpx.Client() as client:
        for i, p in enumerate(proponents, 1):
            name = p["organization_name"]
            vat = p["vat_number"]
            print(f"[{i:3}/{total}] {name} ({vat}) ...", end=" ", flush=True)

            try:
                request_id = submit(client, vat)
                if not request_id:
                    print("ERROR: no request_id")
                    results.append(
                        {"organization_name": name, "vat_number": vat, "status": "error", "error": "no request_id"}
                    )
                    continue

                result = poll(client, request_id)
                status = result.get("status")
                print(status)
                results.append(
                    {
                        "organization_name": name,
                        "vat_number": vat,
                        "request_id": request_id,
                        **result,
                    }
                )
            except Exception as e:
                print(f"EXCEPTION: {e}")
                results.append({"organization_name": name, "vat_number": vat, "status": "error", "error": str(e)})

            # Save progress after each query
            OUTPUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2))

    completed = sum(1 for r in results if r.get("status") == "completed")
    errors = sum(1 for r in results if r.get("status") != "completed")
    print(f"\nDone: {completed} completed, {errors} errors/timeouts")
    print(f"Output: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
