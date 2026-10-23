"""Flat, normalised SISTER schema (deduplicate, rebuild, validate, swap).

Runs the SQL files in ``alembic/sql/20261024_flat_normalised/`` in order, inside the revision's single transaction:

1. ``01_dedup.sql``    merge duplicate locations/subjects/documents/owner rows (audit: ``dedup_audit.removed``)
2. ``02_schema.sql``   build the new tables in ``sister_next`` (typed columns, FKs, no JSON/EAV, no 1:1 side tables)
3. ``03_load.sql``     fill them from the legacy tables (comune + Belfiore codes, units, subjects, rights, ...)
4. ``04_validate.sql`` reconcile counts, links and totals; any mismatch aborts everything
5. ``05_views.sql``    flat read views
6. ``06_swap.sql``     legacy -> ``sister_legacy``, new -> ``sister``; raw payloads live in ``sister_archive``

Nothing is dropped: the previous layout stays in ``sister_legacy``. Take a backup first anyway (step 1 deletes rows).
Downgrade swaps the legacy layout back (``07_down.sql``); data written to the new layout in between is lost.
Design, mapping and application impact: ``docs/database_design.md``.
"""

from pathlib import Path
from typing import Sequence, Union

from alembic import op

revision: str = "20261024_flat_normalised"
down_revision: Union[str, None] = "20261023_normalize_catalogs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql" / "20261024_flat_normalised"
UP_STEPS = ("01_dedup.sql", "02_schema.sql", "03_load.sql", "04_validate.sql", "05_views.sql", "06_swap.sql")
DOWN_STEPS = ("07_down.sql",)


def _run(steps: Sequence[str]) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This SISTER schema migration requires PostgreSQL")
    # A raw cursor sends each file as one simple-protocol query: several statements, DO blocks and a literal % all work.
    cursor = bind.connection.cursor()
    try:
        for name in steps:
            cursor.execute((SQL_DIR / name).read_text(encoding="utf-8"))
    finally:
        cursor.close()


def upgrade() -> None:
    _run(UP_STEPS)


def downgrade() -> None:
    _run(DOWN_STEPS)
