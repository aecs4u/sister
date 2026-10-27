#!/usr/bin/env python3
"""Remove from a markdown list of codici (``codici_fiscali.all.md``) the ones SISTER has already answered for Fabbricati and Terreni.

    ../.venv/bin/python scripts/prune_code_list.py /mnt/mobile/data/aecs4u.it/sister/codici_fiscali.all.md [--dry-run]

Available = a request with ``cadastre_type`` E (or none) that found something or got SISTER's own "nessuna corrispondenza", an outcome
"too many results", or a stored subject visura (``vs_*``). An answer for Terreni alone does not count. The file is rewritten in place
(a copy goes to ``backups/`` next to it), rows are renumbered and the totals in the heading are updated.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from run_in_session import answered_codes  # noqa: E402  (same definition of "answered" as the runner)

ROW = re.compile(r"^\| (\d+) \| `([A-Z0-9]+)` \|(.*)$")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("list_file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    answered = asyncio.run(answered_codes())
    lines = args.list_file.read_text(encoding="utf-8").splitlines()
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
    if args.dry_run:
        return 0
    backup = args.list_file.parent / "backups" / f"{args.list_file.stem}.{datetime.now():%Y%m%d_%H%M}.md"
    backup.parent.mkdir(exist_ok=True)
    shutil.copy2(args.list_file, backup)
    args.list_file.write_text(text, encoding="utf-8")
    print(f"copia precedente: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
