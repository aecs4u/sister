#!/usr/bin/env python3
"""Build a resumable SISTER plan from a Markdown tax-code list or parcel-query table.

Tax-code lists use the ``codici_fiscali.all.md`` format. PVP parcel lists use a Markdown table marked
``<!-- sister-batch-table:v1 -->`` with the same column names as the batch query fields.

16-character codes become ``soggetto`` queries; 11-digit VAT numbers become ``azienda`` queries that read the
nationwide company results table directly. Generated PVP parcel rows retain their query metadata and run through the
same ``scripts/run_list_in_session.sh`` workflow.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sister.fiscal_code import valid_codice_fiscale, valid_partita_iva  # noqa: E402
from sister.query_forms import get_query_form, param_names, validate_params  # noqa: E402

PVP_TABLE_MARKER = "<!-- sister-batch-table:v1 -->"


def _markdown_cells(line: str) -> list[str]:
    """Split a generated Markdown table row; literal pipes are HTML-escaped by the converter."""
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return []
    return [html.unescape(cell.strip().replace("<br>", "\n")) for cell in stripped[1:-1].split("|")]


def _parcel_queries(markdown: str) -> list[dict] | None:
    """Load a marked parcel table, preserving non-query columns as result metadata."""
    if PVP_TABLE_MARKER not in markdown:
        return None
    headers: list[str] | None = None
    queries: list[dict] = []
    for line_number, line in enumerate(markdown.splitlines(), 1):
        cells = _markdown_cells(line)
        if not cells:
            continue
        if headers is None:
            normalized = [cell.strip().lower() for cell in cells]
            if {"command", "provincia", "comune", "foglio", "particella"}.issubset(normalized):
                headers = normalized
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if len(cells) != len(headers):
            raise ValueError(f"{line_number}: riga Markdown con {len(cells)} celle, attese {len(headers)}")
        row = dict(zip(headers, cells))
        query_id = row.pop("id", "").strip() or None
        command = row.pop("command", "").strip()
        spec = get_query_form(command)
        if spec is None:
            raise ValueError(f"{line_number}: comando Markdown sconosciuto: {command!r}")
        allowed = set(param_names(spec.command))
        params = {key: value for key, value in row.items() if key in allowed and value.strip()}
        metadata = {key: value for key, value in row.items() if key not in allowed and value.strip()}
        try:
            validate_params(spec.command, params)
        except ValueError as exc:
            raise ValueError(f"{line_number}: {exc}") from exc
        query = {"command": spec.command, "params": params}
        if query_id:
            query["id"] = query_id
        if metadata:
            query["metadata"] = metadata
        queries.append(query)
    if headers is None or not queries:
        raise ValueError("la tabella Markdown PVP non contiene righe di query")
    return queries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("list_file", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tipo-richiesta", choices=("attualita", "storica"), help="'Tipo richiesta' for person searches")
    parser.add_argument("--name")
    args = parser.parse_args()

    markdown = args.list_file.read_text(encoding="utf-8")
    queries = _parcel_queries(markdown)
    skipped: list[str] = []
    is_parcel_table = queries is not None
    if queries is None:
        codes = list(dict.fromkeys(re.findall(r"^\| \d+ \| `([A-Z0-9]+)` \|", markdown, re.M)))
        # Only the persona fisica form has a tipo-richiesta control.
        extra = {"tipo_richiesta": args.tipo_richiesta} if args.tipo_richiesta else {}
        queries = []
        for code in codes:
            if len(code) == 16 and valid_codice_fiscale(code):
                queries.append({"id": f"pf-{code}", "command": "soggetto", "params": {"codice_fiscale": code, "tipo_catasto": "E", **extra}})
            elif len(code) == 11 and (valid_partita_iva(code) or code.startswith("00000000")):
                queries.append({"id": f"pi-{code}", "command": "azienda", "params": {"identificativo": code, "tipo_catasto": "E"}})
            else:
                skipped.append(code)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    default_name = (
        f"PVP sales parcels — {args.list_file.stem}"
        if is_parcel_table
        else "Codici ancora da richiedere (Fabbricati e Terreni)"
    )
    plan = {
        "version": 1,
        "name": args.name or default_name,
        "description": f"Da {args.list_file.name}. Sola lettura.",
        "options": {
            "wait": True,
            "reuse": "reuse",
            "max_in_flight": 1,
            "retries": 0,
            "on_error": "continue",
            "poll_timeout": 300,
            "output_dir": str(args.output_dir.resolve()),
        },
        "queries": queries,
    }
    (args.output_dir / "batch.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    if skipped:
        print(f"{len(queries)} righe in {args.output_dir / 'batch.json'}; saltati (formato o controllo errato): {skipped}")
    else:
        print(f"{len(queries)} righe in {args.output_dir / 'batch.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
