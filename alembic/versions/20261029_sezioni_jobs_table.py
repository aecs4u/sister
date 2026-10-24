"""Make sure the territory-section extraction job table exists.

``20260925_sezioni_jobs`` creates ``sezioni_extraction_jobs``, but the live database recorded that revision without the
table ever existing, so ``init_db`` (which requires it) refused to start the service. This revision creates it, with the
same definition, only where it is missing; a database that already has it is left untouched.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261029_sezioni_jobs_table"
down_revision: Union[str, None] = "20261028_istat_municipalities"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("sezioni_extraction_jobs"):
        op.create_table(
            "sezioni_extraction_jobs",
            sa.Column("job_id", sa.String(), nullable=False),
            sa.Column("cadastre_type", sa.String(), nullable=False),
            sa.Column("max_provinces", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("progress_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("results_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.PrimaryKeyConstraint("job_id"),
        )
        inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("sezioni_extraction_jobs")}
    if "ix_sezioni_extraction_jobs_status" not in indexes:
        op.create_index("ix_sezioni_extraction_jobs_status", "sezioni_extraction_jobs", ["status"])


def downgrade() -> None:
    # The table may predate this revision (it is also created by 20260925_sezioni_jobs), so it is kept.
    pass
