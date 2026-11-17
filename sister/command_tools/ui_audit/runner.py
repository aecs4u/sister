"""In-process launcher for the existing browser audit scripts."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

from sister.cli._runtime import project_root

AUDIT_DIR = Path(__file__).resolve().parent


def run_audit(name: str, *, argv: list[str] | None = None) -> int:
    """Execute a legacy audit script in this process and map its result flags to an exit code."""
    if name not in {"sweep", "interactions", "demo_purchases", "purchase_paths"}:
        raise ValueError(f"unknown UI audit: {name}")
    script = AUDIT_DIR / f"{name}.py"
    previous_argv = sys.argv
    previous_path = list(sys.path)
    previous_cwd = Path.cwd()
    sys.argv = [str(script), *(argv or [])]
    try:
        root = project_root()
        os.chdir(root)
        try:
            result = runpy.run_path(str(script), run_name="__main__")
        except SystemExit as exc:
            return int(exc.code or 0)
    finally:
        os.chdir(previous_cwd)
        sys.argv = previous_argv
        sys.path[:] = previous_path

    checks = result.get("res")
    if isinstance(checks, dict):
        if any(not (value[0] if isinstance(value, tuple) else value) for value in checks.values()):
            return 1
    summary = result.get("summary")
    if isinstance(summary, dict):
        for variants in summary.values():
            for data in variants.values():
                if data.get("status") != 200 or data.get("error") or data.get("hOverflow"):
                    return 1
    return 0
