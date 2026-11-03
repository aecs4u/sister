"""Unified Recrowd historical-visura batch command.

Choose ``--items proponents`` for missing company subject visure or
``--items properties`` for missing property visure. Each workflow keeps its
own limits and reporting options; arguments after ``--items`` are forwarded
to the selected workflow.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = {
    "proponents": "_recrowd_proponents.py",
    "properties": "_recrowd_properties.py",
}


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description=__doc__,
        add_help=False,
        epilog=(
            "Examples: --items proponents --limit 2; "
            "--items properties --limit-proponents 2 --limit-properties 5."
        ),
    )
    parser.add_argument(
        "--items",
        choices=WORKFLOWS,
        help="Which Recrowd item list to process: proponents or properties.",
    )

    if raw_args in ([], ["-h"], ["--help"]):
        parser.print_help()
        return 0

    args, forwarded = parser.parse_known_args(raw_args)
    if not args.items:
        parser.error("--items is required (choose proponents or properties)")

    workflow = Path(__file__).resolve().with_name(WORKFLOWS[args.items])
    return subprocess.run([sys.executable, str(workflow), *forwarded], cwd=PROJECT_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
