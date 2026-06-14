#!/usr/bin/env python3
"""Download missing P7M files for PDFs that have no companion signed XML.

For each PDF in the documents directory without a paired .p7m, extracts the
query parameters from the PDF text and re-runs the corresponding SISTER query:
  - visura per soggetto  → POST /visura/soggetto
  - visura per immobile  → POST /visura

Progress is saved after each query so the script can be re-run safely.
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

SISTER_URL = "http://localhost:8025"
DOCS_DIR = Path("/mnt/mobile/data/aecs4u.it/sister/documents")
OUTPUT_PATH = Path(__file__).parent.parent / "outputs" / f"download_p7m_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
POLL_INTERVAL = 5
TIMEOUT = 300

# ── PDF text extraction ──────────────────────────────────────────────────────

_CF_RE = re.compile(r"CF:\s*([A-Z0-9]{16})")
_SOGG_NAME_RE = re.compile(r"Soggetto richiesto:\s*([A-ZÀÈÉÌÒÙ][A-Za-zàèéìòùÀÈÉÌÒÙ '\-]+?)\s+(?:nato|nata)\s+")
_TIPO_VIS_RE = re.compile(r"Visura (?:storica|attuale|sintetica) per (soggetto|immobile)")
_PROV_RE = re.compile(r"Direzione Provinciale di ([A-ZÀÈÉÌÒÙ][A-Za-zàèéìòù]+)")
_FOGLIO_RE = re.compile(r"Sez\. Urb\. \w+ Foglio\s+(\d+)\s+Particella\s+(\d+)\s+Subalterno\s+(\d+)")
_FOGLIO_FALLBACK_RE = re.compile(r"Foglio\s+(\d+)\s+Particella\s+(\S+?)(?:\s+Subalterno\s+(\S+))?(?:\s|$)")
_COMUNE_RE = re.compile(r"Comune di\s+([A-ZÀÈÉÌÒÙ][A-Za-zàèéìòùÀÈÉÌÒÙ '\-]+?)\s+\(")
_TIPO_CAT_RE = re.compile(r"Catasto (fabbricati|terreni)")
_PROV_SIGLA_RE = re.compile(r"in tutta la provincia di ([A-Z]{2})")


def _pdf_text(path: Path) -> str:
    result = subprocess.run(["pdftotext", str(path), "-"], capture_output=True, text=True)
    return result.stdout


def _parse_pdf(path: Path) -> dict | None:
    txt = _pdf_text(path)
    if not txt.strip():
        return None

    tipo_m = _TIPO_VIS_RE.search(txt)
    tipo_obj = tipo_m.group(1) if tipo_m else None

    prov_m = _PROV_RE.search(txt)
    provincia = prov_m.group(1) if prov_m else ""

    if tipo_obj == "soggetto":
        cf_m = _CF_RE.search(txt)
        name_m = _SOGG_NAME_RE.search(txt)
        # Province from "siti in tutta la provincia di RA" line
        prov_sigla_m = _PROV_SIGLA_RE.search(txt)
        return {
            "tipo": "soggetto",
            "cf": cf_m.group(1) if cf_m else None,
            "name": name_m.group(1).strip() if name_m else "",
            "provincia": prov_sigla_m.group(1) if prov_sigla_m else provincia,
        }

    if tipo_obj == "immobile":
        foglio_m = _FOGLIO_RE.search(txt) or _FOGLIO_FALLBACK_RE.search(txt)
        comune_m = _COMUNE_RE.search(txt)
        cat_m = _TIPO_CAT_RE.search(txt)
        if not foglio_m:
            return None
        return {
            "tipo": "immobile",
            "provincia": provincia,
            "comune": comune_m.group(1) if comune_m else "",
            "foglio": foglio_m.group(1),
            "particella": foglio_m.group(2),
            "subalterno": foglio_m.group(3) if len(foglio_m.groups()) >= 3 else "",
            "tipo_catasto": cat_m.group(1)[0].upper() if cat_m else "F",
        }

    return None


# ── Query builders ───────────────────────────────────────────────────────────


def collect_queries(docs_dir: Path) -> list[dict]:
    """Scan PDFs without companion P7M and build a deduplicated query list."""
    sogg_seen: set[tuple] = set()
    imm_seen: set[tuple] = set()
    queries = []

    for pdf in sorted(docs_dir.glob("*.pdf")):
        p7m = docs_dir / f"{pdf.stem}.p7m"
        if p7m.exists():
            continue  # already has signed XML

        info = _parse_pdf(pdf)
        if info is None:
            print(f"  SKIP (unreadable): {pdf.name}", flush=True)
            continue

        if info["tipo"] == "soggetto":
            cf = info.get("cf")
            if not cf:
                print(f"  SKIP (no CF): {pdf.name}", flush=True)
                continue
            key = (cf, info.get("provincia", ""))
            if key in sogg_seen:
                continue
            sogg_seen.add(key)
            queries.append(
                {
                    "endpoint": "/visura/soggetto",
                    "payload": {
                        "codice_fiscale": cf,
                        "tipo_catasto": "E",
                        "provincia": info["provincia"] or None,
                    },
                    "label": f"{info['name']} ({cf}) [{info['provincia']}]",
                    "source_pdf": pdf.name,
                }
            )

        elif info["tipo"] == "immobile":
            key = (info["foglio"], info["particella"], info.get("subalterno", ""))
            if key in imm_seen:
                continue
            imm_seen.add(key)
            sub = info.get("subalterno") or None
            sub_label = f" SUB{sub}" if sub else ""
            queries.append(
                {
                    "endpoint": "/visura",
                    "payload": {
                        "provincia": info["provincia"],
                        "comune": info["comune"],
                        "foglio": info["foglio"],
                        "particella": info["particella"],
                        "subalterno": sub,
                        "tipo_catasto": info["tipo_catasto"],
                    },
                    "label": f"{info['provincia']} FG{info['foglio']} PT{info['particella']}{sub_label}",
                    "source_pdf": pdf.name,
                }
            )

    return queries


# ── HTTP helpers ─────────────────────────────────────────────────────────────


def submit(client: httpx.Client, endpoint: str, payload: dict) -> str | None:
    resp = client.post(f"{SISTER_URL}{endpoint}", json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json().get("request_id")


def poll(client: httpx.Client, request_id: str) -> dict:
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        resp = client.get(f"{SISTER_URL}/visura/{request_id}", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") in ("completed", "error", "failed"):
            return data
        time.sleep(POLL_INTERVAL)
    return {"status": "timeout", "request_id": request_id}


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    if not DOCS_DIR.exists():
        print(f"Documents directory not found: {DOCS_DIR}", file=sys.stderr)
        sys.exit(1)

    print("Scanning for PDFs without companion P7M...", flush=True)
    queries = collect_queries(DOCS_DIR)

    if not queries:
        print("Nothing to do — all PDFs have a companion P7M.")
        return

    print(f"\nFound {len(queries)} unique queries to run:")
    for q in queries:
        print(f"  {q['endpoint']}  {q['label']}")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    results = []

    with httpx.Client() as client:
        for i, q in enumerate(queries, 1):
            label = q["label"]
            print(f"\n[{i:2}/{len(queries)}] {label} ...", end=" ", flush=True)
            try:
                request_id = submit(client, q["endpoint"], q["payload"])
                if not request_id:
                    print("ERROR: no request_id")
                    results.append({**q, "status": "error", "error": "no request_id"})
                    continue
                result = poll(client, request_id)
                status = result.get("status")
                print(status)
                results.append({**q, "request_id": request_id, **result})
            except Exception as e:
                print(f"EXCEPTION: {e}")
                results.append({**q, "status": "error", "error": str(e)})

            OUTPUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2))

    completed = sum(1 for r in results if r.get("status") == "completed")
    errors = len(results) - completed
    print(f"\nDone: {completed} completed, {errors} errors/timeouts")
    print(f"Output: {OUTPUT_PATH}")
    print("\nRun 'Aggiorna' in /web/documents to index the new P7M files.")


if __name__ == "__main__":
    main()
