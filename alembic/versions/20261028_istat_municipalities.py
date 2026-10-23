"""Use the shared ISTAT municipality foreign table for SISTER location keys."""

from pathlib import Path
from typing import Sequence, Union

from alembic import op

revision: str = "20261028_istat_municipalities"
down_revision: Union[str, None] = "20261027_flat_schema_app_fit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql" / revision


def _schema_names() -> tuple[str, str]:
    options = op.get_context().opts
    sister_schema = options.get("sister_schema", "sister")
    istat_schema = options.get("istat_schema", "istat")
    preparer = op.get_bind().dialect.identifier_preparer
    return preparer.quote_schema(sister_schema), preparer.quote_schema(istat_schema)


def _run(name: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This SISTER schema migration requires PostgreSQL")
    sister_schema, istat_schema = _schema_names()
    sql = (SQL_DIR / name).read_text(encoding="utf-8")
    sql = sql.replace("__SISTER_SCHEMA__", sister_schema).replace("__ISTAT_SCHEMA__", istat_schema)
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
    bind = op.get_bind()
    sister_schema, _istat_schema = _schema_names()
    sql_dir = Path(__file__).resolve().parent.parent / "sql" / "20261024_flat_normalised"
    sql = (sql_dir / "05_views.sql").read_text(encoding="utf-8")
    sql = sql.replace("SET LOCAL search_path = sister, public;", "")
    sql = sql.replace("sister_next", sister_schema)
    cursor = bind.connection.cursor()
    try:
        cursor.execute(sql)
    finally:
        cursor.close()
