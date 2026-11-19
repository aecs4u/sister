"""Select SISTER's database connection from the application's environment."""

import os


def database_dsn_suffix(environment: str | None = None) -> str:
    """Return the DSN suffix for dev, isolated test, staging, or production."""
    selected = (environment if environment is not None else os.getenv("ENVIRONMENT", "development")).strip().lower()
    if selected in {"production", "prod", "staging", "stage"}:
        return "NEON"
    if selected in {"test", "testing"}:
        return "TEST"
    if selected in {"development", "dev", "local", "localhost"}:
        return "LOCALHOST"
    raise ValueError(
        "ENVIRONMENT must be development, testing, staging, or production "
        f"(received {selected!r})"
    )


def database_dsn_setting() -> str:
    """Select one explicit DSN without falling back to another environment."""
    return f"SISTER_DATABASE_DSN_{database_dsn_suffix()}"


def get_database_dsn(*, required: bool = False) -> str | None:
    """Read only the selected setting; never fall back to a different database."""
    setting = database_dsn_setting()
    dsn = os.getenv(setting, "").strip() or None
    if required and not dsn:
        raise RuntimeError(f"{setting} must be set to a PostgreSQL connection URL")
    return dsn
