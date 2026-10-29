"""Expose cadastral parcel and sheet geometries through SISTER views."""

from pathlib import Path
from typing import Sequence, Union

from alembic import op

revision: str = "20261113_cadastral_geometry_views"
down_revision: Union[str, None] = "20261112_omi_selection_origin"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql" / revision


def _run(name: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("Cadastral geometry views require PostgreSQL")
    options = op.get_context().opts
    sister_schema = options.get("sister_schema", "sister")
    istat_schema = options.get("istat_schema", "istat")
    preparer = bind.dialect.identifier_preparer
    sql = (SQL_DIR / name).read_text(encoding="utf-8")
    sql = sql.replace("__SISTER_SCHEMA__", preparer.quote_schema(sister_schema))
    sql = sql.replace("__ISTAT_SCHEMA__", preparer.quote_schema(istat_schema))
    cursor = bind.connection.cursor()
    try:
        cursor.execute(sql)
    finally:
        cursor.close()


def upgrade() -> None:
    _run("up.sql")
    _run("views.sql")


def downgrade() -> None:
    _run("down.sql")
