"""Schema adjustments found while porting the application to the flat layout.

Opens ``document.file_format``, drops the start/end ordering check on rights and gives the archived XML tree identity ids.
Idempotent; see ``alembic/sql/20261027_flat_schema_app_fit/up.sql``.
"""

from pathlib import Path
from typing import Sequence, Union

from alembic import op

revision: str = "20261027_flat_schema_app_fit"
down_revision: Union[str, None] = "20261026_flat_normalised_fixes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql" / "20261027_flat_schema_app_fit"


def _run(name: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This SISTER schema migration requires PostgreSQL")
    cursor = bind.connection.cursor()
    try:
        cursor.execute((SQL_DIR / name).read_text(encoding="utf-8"))
    finally:
        cursor.close()


def upgrade() -> None:
    _run("up.sql")


def downgrade() -> None:
    _run("down.sql")
