#!/usr/bin/env python3
"""Remove from a Markdown batch list the rows whose answer SISTER (or the document store) already has.

    uv run sister batch prune-list inputs/codici_fiscali.all.md [--dry-run]

Works on every list ``sister batch build-list`` reads (``docs/batch_markdown_format.md``):

* subject rows (``codice_fiscale`` / ``identificativo``): available = a request with ``cadastre_type`` E (or none) that found
  something or got SISTER's own "nessuna corrispondenza", an outcome "too many results", or a stored subject visura (``vs_*``).
  An answer for Terreni alone does not count;
* parcel rows (comune, foglio, particella, optional subalterno and ``tipo_catasto``): available = a stored visura
  (``visura``, ``visura_fabbricati``, ``visura_terreni``, ``visura_soggetto``) of that comune/foglio/particella/subalterno
  and catasto. Rows whose ids differ only by a final ``-F``/``-T`` are alternatives for one identifier of unknown type:
  when one is available, all go;
* other rows stay;
* a row with ``reuse: force`` is repeated on purpose and always stays (the runner also sends it again, whatever the
  service has cached).

The file is rewritten in place (a copy goes to ``backups/`` next to it). Ids are never renumbered, so a journal stays valid.
The older codici lists are renumbered and their totals updated, as before.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from run_in_session import answered_codes  # noqa: E402  (same definition of "answered" as the runner)

from sister.batch_markdown import is_table_markdown, parse_markdown  # noqa: E402

ROW = re.compile(r"^\| (\d+) \| `([A-Z0-9]+)` \|(.*)$")


DOCUMENT_TYPES = "('visura', 'visura_fabbricati', 'visura_terreni', 'visura_soggetto')"
ALTERNATIVE = re.compile(r"^(.*)-[FT]$")


def _letters(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", text)


def _number(value: str | None) -> str:
    value = (value or "").strip()
    return "" if value in {"", "—", "-"} else value.lstrip("0") or "0"


async def stored_parcels() -> set[tuple[str, str, str, str, str, str]]:
    """(comune, province, foglio, particella, subalterno, catasto) of every stored property visura."""
    from sqlalchemy import text

    from sister.database import _get_session_factory

    async with _get_session_factory()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT comune, province_code, sheet, parcel, subunit, cadastre_type "
                    f"FROM v_document_cadastral_unit WHERE document_type IN {DOCUMENT_TYPES}"
                )
            )
        ).all()
    return {(_letters(c), (p or "").upper(), _number(f), _number(pa), _number(su), t) for c, p, f, pa, su, t in rows}


def parcel_available(params: dict, stored: set) -> bool | None:
    """True/False for a parcel row, None when the row is not a parcel query."""
    if not (params.get("comune") and params.get("foglio") and params.get("particella")):
        return None
    catasto = (params.get("tipo_catasto") or "").upper()
    return any(
        key[0] == _letters(params["comune"])
        and (not params.get("provincia") or key[1] == params["provincia"].upper())
        and key[2:5] == (_number(params["foglio"]), _number(params["particella"]), _number(params.get("subalterno")))
        and (catasto not in {"F", "T"} or key[5] == catasto)
        for key in stored
    )


def available_lines(text: str, answered: set[str], stored: set) -> tuple[list[int], dict[str, int]]:
    """Line numbers of the rows to drop, and how many rows were tested per kind."""
    parsed = parse_markdown(text)
    verdicts: list[bool | None] = []
    counts = {"subject": 0, "parcel": 0, "other": 0}
    for query in parsed.batch.queries:
        params = {key: value for key, value in query.params.items() if value}
        code = str(params.get("codice_fiscale") or params.get("identificativo") or "").strip().upper()
        if query.reuse == "force":  # a row that asks to be repeated on purpose is never "already answered"
            counts["subject" if code else "other"] += 1
            verdicts.append(False)
            continue
        if code:
            counts["subject"] += 1
            verdicts.append(code in answered)
            continue
        verdict = parcel_available(params, stored)
        counts["other" if verdict is None else "parcel"] += 1
        verdicts.append(verdict)
    groups: dict[str, bool] = {}
    for query, verdict in zip(parsed.batch.queries, verdicts):
        match = ALTERNATIVE.match(query.id or "")
        if match and verdict:
            groups[match.group(1)] = True
    drop = []
    for query, verdict, line in zip(parsed.batch.queries, verdicts, parsed.lines):
        match = ALTERNATIVE.match(query.id or "")
        if verdict or (match and groups.get(match.group(1))):
            drop.append(line)
    return drop, counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("list_file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    answered = asyncio.run(answered_codes())
    source = args.list_file.read_text(encoding="utf-8")
    if is_table_markdown(source):
        stored = asyncio.run(stored_parcels())
        drop, counts = available_lines(source, answered, stored)
        kept = [line for number, line in enumerate(source.splitlines(), 1) if number not in set(drop)]
        print(f"{sum(counts.values())} righe ({counts['subject']} soggetti, {counts['parcel']} particelle, "
              f"{counts['other']} altre): {len(drop)} gia' disponibili tolte, {sum(counts.values()) - len(drop)} restano")
        return _write(args, "\n".join(kept) + "\n")
    lines = source.splitlines()
    out, removed, counts, section, number = [], [], {}, None, 0
    for line in lines:
        if line.startswith("## "):
            section, number = len(counts), 0
            counts[section] = 0
        match = ROW.match(line)
        if match:
            if match.group(2) in answered:
                removed.append(match.group(2))
                continue
            number += 1
            counts[section] += 1
            line = f"| {number} | `{match.group(2)}` |{match.group(3)}"
        out.append(line)

    kept = sum(counts.values())
    text = "\n".join(out) + "\n"
    heads = iter(counts.values())
    text = re.sub(r"^(## .*?)\((\d+)\)$", lambda m: f"{m.group(1)}({next(heads)})", text, flags=re.M)
    text = re.sub(r"Totale: \*\*\d+\*\* da interrogare \(\d+ persone fisiche, \d+ partite IVA\)",
                  lambda m: f"Totale: **{kept}** da interrogare ({list(counts.values())[0]} persone fisiche, {list(counts.values())[1]} partite IVA)", text)
    print(f"{len(removed) + kept} righe: {len(removed)} gia' risposte tolte, {kept} restano")
    return _write(args, text)


def _write(args: argparse.Namespace, text: str) -> int:
    if args.dry_run:
        return 0
    backup = args.list_file.parent / "backups" / f"{args.list_file.stem}.{datetime.now():%Y%m%d_%H%M%S}.md"
    backup.parent.mkdir(exist_ok=True)
    shutil.copy2(args.list_file, backup)
    args.list_file.write_text(text, encoding="utf-8")
    print(f"copia precedente: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
