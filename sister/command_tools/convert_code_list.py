#!/usr/bin/env python3
"""Rewrite a legacy codici list (``| # | `CODE` | Nome | Fonte |``) as a Markdown batch file with marked tables.

    uv run sister batch convert-list inputs/codici_fiscali.all.md [-o OUT.md] [--apply]

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


def run(list_file: Path, *, output: Path | None = None, apply: bool = False) -> int:
    """Preview or write a converted list, preserving a backup for in-place edits."""
    source = list_file.read_text(encoding="utf-8")
    if is_table_markdown(source):
        print(f"{list_file} e' gia' un file batch con tabelle marcate")
        return 0
    text = legacy_codes_to_markdown(source)
    try:
        parse_markdown(text)
    except ValueError as exc:  # an empty list has no rows to read back
        print(f"attenzione: il file convertito non si legge ({exc})", file=sys.stderr)
    target = output or list_file
    if not apply:
        print(f"prova a vuoto: sarebbe scritto {target}")
        return 0
    if target == list_file:
        backup = target.parent / "backups" / f"{target.stem}.{datetime.now():%Y%m%d_%H%M%S}.md"
        backup.parent.mkdir(exist_ok=True)
        shutil.copy2(target, backup)
        print(f"copia precedente: {backup}")
    target.write_text(text, encoding="utf-8")
    print(f"scritto {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("list_file", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--dry-run", action="store_false", dest="apply", help="show the destination without writing")
    parser.add_argument("--apply", action="store_true", dest="apply", help="write the converted Markdown file")
    parser.set_defaults(apply=False)
    args = parser.parse_args(argv)
    return run(args.list_file, output=args.output, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
