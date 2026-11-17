"""Prompt for and run the PVP IS Markdown batch in the current SISTER session."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LIST = ROOT / "inputs" / "pvp_sales" / "IS.md"


def run(
    source_path: Path | None = None,
    batch_dir: Path | None = None,
    *,
    prompt: Callable[[str], str] = input,
) -> int:
    """Prompt for an optional list path, then delegate session work to ``run-list``."""
    selected = source_path
    if selected is None:
        value = prompt(f"Markdown list path to process [{DEFAULT_LIST}]: ").strip()
        selected = Path(value) if value else DEFAULT_LIST
    if not selected.is_absolute():
        selected = ROOT / selected
    selected = selected.resolve()
    if not selected.is_file():
        print(f"Source list not found: {selected}")
        return 2

    from .run_list import run as run_list

    return run_list(list_file=selected, batch_dir=batch_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_path", nargs="?", type=Path)
    parser.add_argument("batch_dir", nargs="?", type=Path)
    args = parser.parse_args(argv)
    return run(args.source_path, args.batch_dir)


if __name__ == "__main__":
    raise SystemExit(main())
