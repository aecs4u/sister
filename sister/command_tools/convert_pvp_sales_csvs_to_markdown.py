#!/usr/bin/env python3
"""Convert per-province PVP parcel CSVs into Markdown lists for run_list_in_session.sh."""

from __future__ import annotations

import argparse
import csv
import html
import sys
from pathlib import Path


TABLE_MARKER = "<!-- sister-batch-table:v1 -->"
OUTPUT_COLUMNS = (
    "id",
    "command",
    "provincia",
    "comune",
    "foglio",
    "particella",
    "subalterno",
    "tipo_catasto",
    "sezione",
    "vista",
    "scope_note",
)
REQUIRED_COLUMNS = {"command", "provincia", "comune", "foglio", "particella", "tipo_catasto"}


def markdown_cell(value: str) -> str:
    """Encode a value so pipes and markup remain a single table cell."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return "<br>".join(html.escape(line, quote=False).replace("|", "&#124;") for line in normalized.split("\n"))


def convert_file(source: Path, output_dir: Path) -> int:
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or ())
        missing = REQUIRED_COLUMNS - headers
        if missing:
            raise ValueError(f"{source}: missing required columns: {', '.join(sorted(missing))}")

        rows = list(reader)

    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{source.stem}.md"
    lines = [
        f"# PVP sales parcels — {source.stem}",
        "",
        f"Source: `{source.name}`; {len(rows)} parcel queries.",
        "",
        TABLE_MARKER,
        "| " + " | ".join(OUTPUT_COLUMNS) + " |",
        "|" + "|".join("---" for _ in OUTPUT_COLUMNS) + "|",
    ]
    for number, source_row in enumerate(rows, start=1):
        row = {key: (source_row.get(key) or "").strip() for key in OUTPUT_COLUMNS if key != "id"}
        row["id"] = f"pvp-{source.stem}-{number:06d}"
        lines.append("| " + " | ".join(markdown_cell(row.get(key, "")) for key in OUTPUT_COLUMNS) + " |")
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path, help="directory containing per-province CSV files")
    parser.add_argument("output_dir", type=Path, help="directory for generated Markdown files")
    args = parser.parse_args()

    sources = sorted(args.source_dir.glob("*.csv"))
    if not sources:
        parser.error(f"no CSV files found in {args.source_dir}")

    try:
        counts = [(source, convert_file(source, args.output_dir)) for source in sources]
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        print(f"Conversion failed: {exc}", file=sys.stderr)
        return 1

    print(f"Created {len(counts)} Markdown files with {sum(count for _, count in counts)} parcel queries in {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
