#!/usr/bin/env python3
"""Submit SISTER batch queries from opendata catasto_richiesta JSON files.

Reads catasto_richiesta_*.json files from opendata/data/, maps each to the
appropriate SISTER API endpoint, submits, and polls for completion.
Results are saved to outputs/batch_opendata_<timestamp>.json.
"""

import glob
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import httpx

SISTER_URL = os.getenv("VISURA_API_URL", "http://localhost:8025")
OPENDATA_DIR = "/mnt/mobile/data/aecs4u.it/opendata/data"
OUTPUT_PATH = (
    Path(__file__).parent.parent / "outputs" / f"batch_opendata_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
)
POLL_INTERVAL = 5
TIMEOUT = 300

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parameter parsing helpers
# ---------------------------------------------------------------------------


def _parse_provincia(raw: str | None) -> str | None:
    """'BOLOGNA Territorio-BO' → 'BOLOGNA', 'NAZIONALE-IT' → None."""
    if not raw:
        return None
    if "NAZIONALE" in raw.upper():
        return None
    return raw.split(" Territorio")[0].strip()


def _parse_comune(raw: str | None) -> str | None:
    """'A944#BOLOGNA#0#0' → 'BOLOGNA'."""
    if not raw:
        return None
    parts = raw.split("#")
    return parts[1].strip() if len(parts) > 1 else raw.strip()


def _parse_tipo_catasto(raw: str | None, single_only: bool = False) -> str:
    """'TF' → 'E', 'F' → 'F', 'T' → 'T'. single_only=True for elenco-immobili (no E)."""
    if not raw:
        return "E"
    raw = raw.upper().replace("TF", "E").replace("FT", "E")
    if raw == "E" and single_only:
        return "F"  # elenco-immobili doesn't support E; default to F
    return raw if raw in ("T", "F", "E") else "E"


def _to_str(v) -> str | None:
    return str(int(v)) if isinstance(v, float) and v == int(v) else (str(v) if v is not None else None)


# ---------------------------------------------------------------------------
# File → API call mapping
# ---------------------------------------------------------------------------


def _build_request(endpoint: str, parametri: dict) -> tuple[str, dict] | None:
    """Return (api_path, payload) or None if endpoint is not supported."""
    p = parametri

    if endpoint in ("elenco_immobili", "prospetto_catastale"):
        # Both use /visura (per-immobile search)
        provincia = _parse_provincia(p.get("provincia"))
        comune = _parse_comune(p.get("comune"))
        if not provincia or not comune:
            return None
        payload = {
            "tipo_catasto": _parse_tipo_catasto(p.get("tipo_catasto")),
            "provincia": provincia,
            "comune": comune,
            "foglio": _to_str(p.get("foglio")),
            "particella": _to_str(p.get("particella")),
        }
        if p.get("subalterno") is not None:
            payload["subalterno"] = _to_str(p["subalterno"])
        if p.get("sezione"):
            payload["sezione"] = str(p["sezione"])
        return "/visura", {k: v for k, v in payload.items() if v is not None}

    if endpoint in ("ricerca_persona_fisica", "ricerca_nazionale_pf"):
        cf = p.get("cf_piva")
        if not cf:
            return None
        payload: dict = {
            "codice_fiscale": str(cf).strip(),
            "tipo_catasto": _parse_tipo_catasto(p.get("tipo_catasto")),
        }
        provincia = _parse_provincia(p.get("provincia"))
        if provincia:
            payload["provincia"] = provincia
        return "/visura/soggetto", payload

    if endpoint in ("ricerca_persona_giuridica", "ricerca_nazionale_pg"):
        identificativo = p.get("cf_piva")
        if not identificativo:
            return None
        payload = {
            "identificativo": str(identificativo).strip(),
            "tipo_catasto": _parse_tipo_catasto(p.get("tipo_catasto")),
        }
        provincia = _parse_provincia(p.get("provincia"))
        if provincia:
            payload["provincia"] = provincia
        return "/visura/persona-giuridica", payload

    return None  # unsupported endpoint


def load_requests(opendata_dir: str) -> list[dict]:
    """Load all catasto_richiesta files and return a list of request dicts."""
    files = sorted(glob.glob(os.path.join(opendata_dir, "catasto_richiesta*.json")))
    requests = []
    seen = set()

    for filepath in files:
        raw = json.load(open(filepath))
        items = raw if isinstance(raw, list) else [raw.get("data", raw)]

        for item in items:
            if not isinstance(item, dict):
                continue
            endpoint = item.get("endpoint")
            parametri = item.get("parametri", {})
            opendata_id = item.get("id", Path(filepath).stem)

            result = _build_request(endpoint, parametri)
            if result is None:
                continue

            api_path, payload = result

            # Deduplicate by (api_path, payload)
            key = (api_path, json.dumps(payload, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)

            requests.append(
                {
                    "opendata_id": opendata_id,
                    "endpoint": endpoint,
                    "api_path": api_path,
                    "payload": payload,
                    "source_file": Path(filepath).name,
                }
            )

    return requests


# ---------------------------------------------------------------------------
# Submit and poll
# ---------------------------------------------------------------------------


def _poll(client: httpx.Client, request_id: str) -> dict:
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        resp = client.get(f"{SISTER_URL}/visura/{request_id}", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") in ("completed", "error", "failed"):
            return data
        time.sleep(POLL_INTERVAL)
    return {"status": "timeout", "request_id": request_id}


def run(opendata_dir: str) -> None:
    requests = load_requests(opendata_dir)
    total = len(requests)
    log.info("Loaded %d unique requests from %s", total, opendata_dir)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    results = []

    with httpx.Client() as client:
        for i, req in enumerate(requests, 1):
            label = f"[{i:3}/{total}] {req['endpoint']:35s} {json.dumps(req['payload'])[:80]}"
            print(label, end=" ... ", flush=True)

            try:
                resp = client.post(
                    f"{SISTER_URL}{req['api_path']}",
                    json=req["payload"],
                    timeout=30,
                )
                resp.raise_for_status()
                submit_data = resp.json()
                request_id = submit_data.get("request_id")

                if not request_id:
                    print("ERROR: no request_id")
                    results.append({**req, "status": "error", "error": "no request_id"})
                    continue

                result = _poll(client, request_id)
                status = result.get("status")
                print(status)
                results.append({**req, "request_id": request_id, **result})

            except Exception as e:
                print(f"EXCEPTION: {e}")
                results.append({**req, "status": "error", "error": str(e)})

            OUTPUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2))

    completed = sum(1 for r in results if r.get("status") == "completed")
    errors = sum(1 for r in results if r.get("status") != "completed")
    log.info("\nDone — %d completed, %d errors/timeouts", completed, errors)
    log.info("Output: %s", OUTPUT_PATH)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opendata-dir", default=OPENDATA_DIR)
    args = parser.parse_args()
    run(args.opendata_dir)
