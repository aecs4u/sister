#!/usr/bin/env python3
"""Backfill immobili and intestati tables from existing output JSON files.

Reads all response JSON files from the outputs directory and populates
visura_requests, visura_responses, immobili, and intestati tables.
Safe to re-run: existing rows are replaced (upsert behaviour in save_response).
"""

import asyncio
import glob
import json
import logging
import os
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from sister.database import _get_session_factory, init_db, save_response
from sister.db_models import VisuraRequest

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

OUTPUTS_DIR = os.getenv(
    "SISTER_OUTPUTS_DIR",
    str(Path(__file__).parent.parent / "outputs"),
)

# Map request_id prefix → request_type
_PREFIX_TO_TYPE = {
    "req": "visura",
    "pnf": "persona_giuridica",
    "soggetto": "soggetto",
    "intestati": "intestati",
    "eimm": "elenco_immobili",
    "ipotecaria": "ispezione_ipotecaria",
}


def _parse_request_id(request_id: str) -> tuple[str, str]:
    """Return (request_type, tipo_catasto) from a request_id like 'pnf_E_abc123'."""
    parts = request_id.split("_")
    prefix = parts[0]
    # tipo_catasto is the part right after the prefix if it's a single letter
    tipo_catasto = parts[1] if len(parts) > 1 and len(parts[1]) == 1 and parts[1] in "TFE" else "E"
    request_type = _PREFIX_TO_TYPE.get(prefix, prefix)
    return request_type, tipo_catasto


async def _ensure_request(session_factory, request_id: str, tipo_catasto: str, request_type: str) -> None:
    """Insert a minimal visura_requests row if it doesn't exist."""
    from sqlalchemy import text

    async with session_factory() as session:
        existing = await session.execute(
            text("SELECT request_id FROM visura_requests WHERE request_id = :rid"),
            {"rid": request_id},
        )
        if existing.fetchone():
            return
        req = VisuraRequest(
            request_id=request_id,
            request_type=request_type,
            tipo_catasto=tipo_catasto,
            provincia="",
            comune="",
            foglio="",
            particella="",
        )
        session.add(req)
        await session.commit()


async def backfill(outputs_dir: str) -> None:
    await init_db()
    session_factory = _get_session_factory()

    files = sorted(glob.glob(os.path.join(outputs_dir, "*.json")))
    log.info("Found %d JSON files in %s", len(files), outputs_dir)

    imported = skipped = errors = 0

    for filepath in files:
        filename = os.path.basename(filepath)
        try:
            raw = json.load(open(filepath))
        except Exception as e:
            log.warning("  SKIP (parse error) %s: %s", filename, e)
            errors += 1
            continue

        if not isinstance(raw, dict) or "request_id" not in raw:
            log.debug("  SKIP (no request_id) %s", filename)
            skipped += 1
            continue

        request_id: str = raw["request_id"]
        success: bool = bool(raw.get("success", False))
        tipo_catasto: str = raw.get("tipo_catasto", "E") or "E"
        data: dict | None = raw.get("data")
        error: str | None = raw.get("error")

        # Only import successful responses with actual data
        if not success or not data:
            log.debug("  SKIP (no data / failed) %s", filename)
            skipped += 1
            continue

        request_type, _ = _parse_request_id(request_id)

        try:
            await _ensure_request(session_factory, request_id, tipo_catasto, request_type)
            await save_response(
                request_id=request_id,
                success=success,
                tipo_catasto=tipo_catasto,
                data=data,
                error=error,
            )
            immobili_count = len(data.get("immobili", []))
            intestati_count = len(data.get("intestati", []))
            log.info(
                "  OK  %-60s  immobili=%-3d  intestati=%-3d",
                request_id,
                immobili_count,
                intestati_count,
            )
            imported += 1
        except Exception as e:
            log.error("  ERR %s: %s", request_id, e)
            errors += 1

    log.info("\nDone — imported: %d, skipped: %d, errors: %d", imported, skipped, errors)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outputs-dir", default=OUTPUTS_DIR, help="Path to outputs directory")
    args = parser.parse_args()
    asyncio.run(backfill(args.outputs_dir))
