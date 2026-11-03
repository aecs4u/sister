#!/usr/bin/env python3
"""Rewrite a legacy codici list (``| # | `CODE` | Nome | Fonte |``) as a Markdown batch file with marked tables.

    uv run sister batch convert-list inputs/codici_fiscali.all.md [-o OUT.md]

The result is read by ``sister batch build-list`` / ``run-list`` / ``prune-list`` and by ``sister query batch`` like any
other batch file (``docs/batch_markdown_format.md``). Without ``-o`` the file is rewritten in place and the previous
version is copied to ``backups/`` next to it.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sister.batch_markdown import is_table_markdown, legacy_codes_to_markdown, parse_markdown  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("list_file", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()

    source = args.list_file.read_text(encoding="utf-8")
    if is_table_markdown(source):
        print(f"{args.list_file} e' gia' un file batch con tabelle marcate")
        return 0
    text = legacy_codes_to_markdown(source)
    try:
        parse_markdown(text)
    except ValueError as exc:  # an empty list has no rows to read back
        print(f"attenzione: il file convertito non si legge ({exc})", file=sys.stderr)
    target = args.output or args.list_file
    if target == args.list_file:
        backup = target.parent / "backups" / f"{target.stem}.{datetime.now():%Y%m%d_%H%M%S}.md"
        backup.parent.mkdir(exist_ok=True)
        shutil.copy2(target, backup)
        print(f"copia precedente: {backup}")
    target.write_text(text, encoding="utf-8")
    print(f"scritto {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
