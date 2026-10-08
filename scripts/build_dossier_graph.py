#!/usr/bin/env python3
"""Build the ownership graph ("full dossier") around a set of seed persons/companies.

Starting from codici fiscali / partite IVA it repeats, until no new node is found:

  * owner → properties   every immobile of the owner, with its details (ubicazione, classamento,
                          consistenza, rendita, titolarità);
  * property → owners    every intestato of each of those immobili (identity, share), which become new
                          owners to expand in turn.

Everything goes through the running SISTER service (``POST /visura/soggetto-immobili`` with
``con_intestati=1``): no document is requested and no CAPTCHA is involved, but the AdE session must be
logged in (``scripts/ade_login.py``). The graph is saved after every expansion, so an interrupted run
(session expired, Ctrl-C, ``--max-requests`` reached) continues with ``--resume <graph.json>``.

    ../.venv/bin/python scripts/build_dossier_graph.py --seed PGGPLA52C15H199K
    ../.venv/bin/python scripts/build_dossier_graph.py --from-batch outputs/pdf_parseable_batch
    ../.venv/bin/python scripts/build_dossier_graph.py --resume <dossier_graphs>/graph_X.json

Exit status 3 means the SISTER session expired: log in again and resume.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT.parent / ".env", override=False)

BASE_URL = os.getenv("SISTER_URL", "http://localhost:8025")
API_KEY = os.getenv("VISURA_API_KEY") or os.getenv("API_KEY", "")
GRAPHS_DIR = Path(
    os.getenv("SISTER_DOSSIER_GRAPHS_DIR")
    or Path(os.getenv("SISTER_FILES_BASE", ROOT / "data")).parent / "dossier_graphs"
)

ID_RE = re.compile(r"^(?:[A-Z0-9]{16}|\d{11})$")
POLL_SECONDS = 3
REQUEST_TIMEOUT = 900


class SessionExpired(RuntimeError):
    """The SISTER session is gone: log in again and resume."""


# ---------------------------------------------------------------------------
# Pure helpers (unit tested)
# ---------------------------------------------------------------------------


def is_company(ident: str) -> bool:
    return len(ident) == 11 and ident.isdigit()


def property_key(row: dict) -> str:
    """Stable key of an immobile from a listing row (uses the portal's visImmSel value when present).

    ``visImmSel`` looks like ``634568#634568#F#RA/103#1714#H199##2# #RAVENNA``:
    id, id, catasto, provincia/foglio, particella, codice comune, sezione, subalterno, ..., comune.
    """
    parts = [part.strip() for part in (row.get("visImmSel") or "").split("#")]
    if len(parts) >= 8:
        province, _, foglio = parts[3].partition("/")
        return "|".join([parts[2], province, parts[5], foglio, parts[4], parts[6], parts[7]])
    province, _, foglio = (row.get("Foglio") or "").partition("/")
    return "|".join(
        [row.get("Catasto", ""), province, "", foglio or province, row.get("Particella", ""), "", row.get("Sub", "")]
    )


def owner_key(owner: dict) -> str:
    ident = (owner.get("codice_fiscale") or "").strip().upper()
    return ident or f"NOID:{(owner.get('nome') or '').strip().upper()}"


def comune_from_ubicazione(ubicazione: str) -> str:
    match = re.match(r"\s*([^()]+?)\s*\(([A-Z]{2})\)", ubicazione or "")
    return match.group(1).strip() if match else ""


def harvest_ids(node) -> set[str]:
    """Every codice fiscale / partita IVA found in a batch result JSON (any nesting)."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and key.lower().replace(" ", "_") in {
                "codice_fiscale",
                "soggetto",
                "identificativo",
            }:
                if ID_RE.match(value.strip().upper()):
                    found.add(value.strip().upper())
            else:
                found |= harvest_ids(value)
    elif isinstance(node, list):
        for value in node:
            found |= harvest_ids(value)
    return found


class Graph:
    """Nodes, ownership edges and the expansion queue; serialisable for resuming."""

    def __init__(self, data: dict | None = None):
        data = data or {}
        self.nodes: dict[str, dict] = data.get("nodes", {})
        self.edges: dict[str, dict] = data.get("edges", {})
        self.queue: list[str] = data.get("queue", [])
        self.expanded: dict[str, dict] = data.get("expanded", {})
        self.meta: dict = data.get("meta", {})

    def add_owner(self, owner: dict, depth: int) -> str:
        key = owner_key(owner)
        node = self.nodes.setdefault(
            key,
            {
                "type": "company" if is_company(key) else "person",
                "id": key,
                "depth": depth,
            },
        )
        for field in ("nome", "sesso", "luogo_nascita", "data_nascita", "sede"):
            if owner.get(field) and not node.get(field):
                node[field] = owner[field]
        node["depth"] = min(node.get("depth", depth), depth)
        if key not in self.expanded and key not in self.queue and not key.startswith("NOID:"):
            self.queue.append(key)
        return key

    def add_property(self, row: dict) -> str:
        key = property_key(row)
        node = self.nodes.setdefault(key, {"type": "property", "id": key})
        for field in (
            "Catasto",
            "Ubicazione",
            "Foglio",
            "Particella",
            "Sub",
            "Classamento",
            "Classe",
            "Consistenza",
            "Rendita",
            "Partita",
        ):
            if row.get(field):
                node[field] = row[field]
        node["provincia"] = row.get("provincia") or node.get("provincia", "")
        node["provincia_nome"] = row.get("provincia_nome") or node.get("provincia_nome", "")
        node["comune"] = comune_from_ubicazione(row.get("Ubicazione", "")) or node.get("comune", "")
        return key

    def add_edge(self, owner: str, prop: str, titolarita: str = "", quota: str = "") -> None:
        edge = self.edges.setdefault(f"{owner}->{prop}", {"owner": owner, "property": prop})
        if titolarita and not edge.get("titolarita"):
            edge["titolarita"] = titolarita
        if quota and not edge.get("quota"):
            edge["quota"] = quota

    def absorb(self, ident: str, immobili: list[dict], depth: int) -> dict:
        """Merge one owner's property list (with the owners of each property) into the graph."""
        new_owners = 0
        for row in immobili:
            prop = self.add_property(row)
            # the expanded owner itself: the row's own Titolarità column
            self.add_edge(ident, prop, row.get("Titolarità", ""))
            for owner in row.get("intestati") or []:
                before = len(self.nodes)
                key = self.add_owner(owner, depth + 1)
                new_owners += len(self.nodes) - before
                self.add_edge(key, prop, owner.get("titolarita", ""), owner.get("quota", ""))
        return {"properties": len(immobili), "new_owners": new_owners}

    def to_json(self) -> dict:
        return {
            "meta": self.meta,
            "nodes": self.nodes,
            "edges": self.edges,
            "queue": self.queue,
            "expanded": self.expanded,
        }

    def summary(self) -> str:
        kinds: dict[str, int] = {}
        for node in self.nodes.values():
            kinds[node["type"]] = kinds.get(node["type"], 0) + 1
        return (
            f"{len(self.nodes)} nodes ({', '.join(f'{v} {k}' for k, v in sorted(kinds.items()))}), "
            f"{len(self.edges)} edges, {len(self.expanded)} expanded, {len(self.queue)} queued"
        )


# ---------------------------------------------------------------------------
# Service access
# ---------------------------------------------------------------------------


def _headers() -> dict:
    return {"X-API-Key": API_KEY} if API_KEY else {}


def fetch_properties(client: httpx.Client, ident: str) -> tuple[list[dict] | None, str]:
    """Run soggetto-immobili (with owners) for one id; returns (immobili, error)."""
    params = {"provincia": "NAZIONALE", "codice_fiscale": ident, "tipo_catasto": "E", "con_intestati": "1"}
    if is_company(ident):
        params["azienda"] = "1"
    response = client.post(f"{BASE_URL}/visura/soggetto-immobili", params=params)
    response.raise_for_status()
    payload = response.json()
    request_id = payload.get("request_id")
    deadline = time.time() + REQUEST_TIMEOUT
    while True:
        if payload.get("status") in ("completed", "cached", "error"):
            break
        if time.time() > deadline:
            return None, "timeout"
        time.sleep(POLL_SECONDS)
        poll = client.get(f"{BASE_URL}/visura/{request_id}")
        if poll.status_code == 404:
            return None, "request_id non trovato"
        poll.raise_for_status()
        payload = poll.json()
    error = payload.get("error") or ""
    if "scadut" in error.lower() or "sessione" in error.lower():
        raise SessionExpired(error)
    if payload.get("status") == "error":
        return None, error or "errore"
    data = payload.get("data") or {}
    if data.get("error"):
        return [], data["error"]
    return data.get("immobili") or [], ""


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def save(graph: Graph, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(graph.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def seeds_from_batch(folder: Path) -> set[str]:
    found: set[str] = set()
    for file in sorted(folder.glob("*.json")):
        try:
            found |= harvest_ids(json.loads(file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", action="append", default=[], help="codice fiscale or partita IVA (repeatable)")
    parser.add_argument("--from-batch", type=Path, help="folder of batch result JSON: seed with every id found")
    parser.add_argument("--resume", type=Path, help="graph JSON to continue")
    parser.add_argument("--out", type=Path, help="graph JSON to write (default: new file in the graphs folder)")
    parser.add_argument("--max-depth", type=int, default=0, help="stop expanding beyond this depth (0 = no limit)")
    parser.add_argument(
        "--max-nodes", type=int, default=0, help="stop when the graph has this many nodes (0 = no limit)"
    )
    parser.add_argument("--retry-errors", action="store_true", help="re-queue ids whose expansion failed earlier")
    parser.add_argument("--max-requests", type=int, default=300, help="portal requests in this run (resumable)")
    args = parser.parse_args()

    graph = Graph(json.loads(args.resume.read_text(encoding="utf-8"))) if args.resume else Graph()
    out = args.out or args.resume or GRAPHS_DIR / f"graph_{datetime.now():%Y%m%d_%H%M%S}.json"
    graph.meta.setdefault("created", datetime.now().isoformat(timespec="seconds"))

    if args.retry_errors:
        for ident in [i for i, info in graph.expanded.items() if "error" in info]:
            graph.expanded.pop(ident)
            graph.queue.append(ident)

    seeds = {seed.strip().upper() for seed in args.seed}
    if args.from_batch:
        seeds |= seeds_from_batch(args.from_batch)
    for seed in sorted(seeds):
        graph.add_owner({"codice_fiscale": seed}, 0)
    print(f"Graph file: {out}\nStart: {graph.summary()}")

    requests_done = 0
    try:
        with httpx.Client(headers=_headers(), timeout=60) as client:
            while graph.queue:
                if args.max_requests and requests_done >= args.max_requests:
                    print(f"Reached --max-requests={args.max_requests}; resume with --resume {out}")
                    break
                if args.max_nodes and len(graph.nodes) >= args.max_nodes:
                    print(f"Reached --max-nodes={args.max_nodes}")
                    break
                ident = graph.queue[0]
                depth = graph.nodes[ident].get("depth", 0)
                if args.max_depth and depth > args.max_depth:
                    graph.queue.pop(0)
                    graph.expanded[ident] = {"skipped": "max-depth"}
                    continue
                print(f"[{requests_done + 1}] expanding {ident} (depth {depth}) ...", end=" ", flush=True)
                immobili, error = fetch_properties(client, ident)
                requests_done += 1
                graph.queue.pop(0)
                graph.expanded[ident] = {}  # before absorbing: the owner appears among its own properties' owners
                if immobili is None:
                    graph.expanded[ident] = {"error": error, "at": datetime.now().isoformat(timespec="seconds")}
                    print(f"ERROR {error}")
                else:
                    info = graph.absorb(ident, immobili, depth)
                    info["at"] = datetime.now().isoformat(timespec="seconds")
                    if error:
                        info["note"] = error
                    graph.expanded[ident] = info
                    print(f"{info['properties']} properties, {info['new_owners']} new owners")
                save(graph, out)
    except SessionExpired as exc:
        save(graph, out)
        print(f"\nSISTER session expired ({exc}). Log in again, then: --resume {out}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        save(graph, out)
        print(f"\nInterrupted. Resume with --resume {out}")
        return 130

    save(graph, out)
    print(f"Done: {graph.summary()}\nSaved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
