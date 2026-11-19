"""Load workspace and SISTER environment defaults for one runtime profile."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

_ENVIRONMENTS = {"development", "testing", "staging", "production"}
_ALIASES = {
    "dev": "development",
    "local": "development",
    "localhost": "development",
    "test": "testing",
    "stage": "staging",
    "prod": "production",
}


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {
        key: value
        for key, value in dotenv_values(path, interpolate=False).items()
        if key is not None and value is not None
    }


def _select_environment(
    process_environment: dict[str, str],
    app_base: dict[str, str],
    workspace_base: dict[str, str],
) -> str:
    if "ENVIRONMENT" in process_environment:
        requested = process_environment["ENVIRONMENT"].strip().lower()
    elif "ENVIRONMENT" in app_base:
        requested = app_base["ENVIRONMENT"].strip().lower()
    elif "ENVIRONMENT" in workspace_base:
        requested = workspace_base["ENVIRONMENT"].strip().lower()
    else:
        requested = "development"

    environment = _ALIASES.get(requested, requested)
    if environment not in _ENVIRONMENTS:
        choices = ", ".join(sorted(_ENVIRONMENTS))
        raise RuntimeError(f"ENVIRONMENT must be one of {choices}; got {requested!r}")
    return environment


def load_environment() -> str:
    """Load workspace defaults, the selected shared profile, then SISTER overrides.

    Existing process variables always win. Workspace profiles carry shared
    settings such as logging; SISTER's base and optional profile files carry
    app-specific settings and may override those shared values.
    """
    app_root = Path(__file__).resolve().parents[1]
    workspace_root = app_root.parent
    process_environment = dict(os.environ)
    workspace_base = _read_env_file(workspace_root / ".env")
    app_base = _read_env_file(app_root / ".env")
    environment = _select_environment(process_environment, app_base, workspace_base)

    layers = (
        workspace_base,
        app_base,
        _read_env_file(workspace_root / f".env.{environment}"),
        _read_env_file(app_root / f".env.{environment}"),
    )
    merged: dict[str, str] = {}
    for layer in layers:
        merged.update(layer)

    # Keep the normal log level when an app is run without the workspace's
    # shared profile files (for example, in a standalone container).
    merged.setdefault("LOG_LEVEL", "DEBUG" if environment in {"development", "testing"} else "INFO")

    # The selector chooses the profile and must remain consistent with it.
    merged["ENVIRONMENT"] = environment
    for key, value in merged.items():
        if key not in process_environment:
            os.environ[key] = value
    os.environ["ENVIRONMENT"] = environment
    return environment
