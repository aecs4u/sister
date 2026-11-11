"""Select SISTER's database connection from the application's environment."""

import os


def database_dsn_setting() -> str:
    """Use Neon in hosted environments and localhost for development/tests."""
    environment = os.getenv("ENVIRONMENT", "development").strip().lower()
    suffix = "NEON" if environment in {"production", "prod", "staging"} else "LOCALHOST"
    return f"SISTER_DATABASE_DSN_{suffix}"


def get_database_dsn(*, required: bool = False) -> str | None:
    """Read only the selected setting; never fall back to a different database."""
    setting = database_dsn_setting()
    dsn = os.getenv(setting, "").strip() or None
    if required and not dsn:
        raise RuntimeError(f"{setting} must be set to a PostgreSQL connection URL")
    return dsn
