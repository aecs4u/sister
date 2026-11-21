"""Expose a plain shared ISTAT ``municipalities`` table as ``ft_municipalities``.

``20261211_foreign_table_ft_prefix`` only renames *foreign* tables, and the app (``init_db``, ``IstatMunicipality``,
the web queries) reads ``ft_municipalities``. Where the shared ISTAT table is a regular table (production), nothing was
renamed and the service refused to start. A view with the expected name fixes that without touching the data; where the
table already is a foreign table named ``ft_municipalities`` (local development) this revision does nothing.
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261212_ft_municipalities_view"
down_revision: Union[str, None] = "20261211_foreign_table_ft_prefix"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _istat_schema() -> str:
    options = op.get_context().opts
    return options.get("istat_schema") or os.getenv("DATABASE_ISTAT_SCHEMA", os.getenv("DATABASE_SCHEMA", "sister"))


def _relkind(bind, schema: str, name: str) -> str | None:
    return bind.execute(
        sa.text(
            "SELECT c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = :name"
        ),
        {"schema": schema, "name": name},
    ).scalar_one_or_none()


def upgrade() -> None:
    bind = op.get_bind()
    schema = _istat_schema()
    if _relkind(bind, schema, "ft_municipalities") is not None:
        return  # already there: a foreign table (development) or an earlier run of this revision
    if _relkind(bind, schema, "municipalities") not in ("r", "p", "v", "m"):
        raise RuntimeError(f"Shared ISTAT table {schema}.municipalities was not found; set DATABASE_ISTAT_SCHEMA.")
    quoted = bind.dialect.identifier_preparer.quote_schema(schema)
    bind.exec_driver_sql(f"CREATE VIEW {quoted}.ft_municipalities AS SELECT * FROM {quoted}.municipalities")


def downgrade() -> None:
    bind = op.get_bind()
    schema = _istat_schema()
    if _relkind(bind, schema, "ft_municipalities") == "v":
        quoted = bind.dialect.identifier_preparer.quote_schema(schema)
        bind.exec_driver_sql(f"DROP VIEW {quoted}.ft_municipalities")
