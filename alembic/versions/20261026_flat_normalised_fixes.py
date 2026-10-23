"""Forward fix for databases that ran the first variant of 20261024_flat_normalised.

Restores the 602 current-ownership markers and the XML header columns that variant dropped, from ``sister_legacy``. Idempotent: a database that ran the corrected 20261024 is left unchanged. Details in the SQL file.
"""

from pathlib import Path
from typing import Sequence, Union

from alembic import op

revision: str = "20261026_flat_normalised_fixes"
down_revision: Union[str, None] = "20261024_flat_normalised"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql" / "20261026_flat_normalised_fixes"


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
