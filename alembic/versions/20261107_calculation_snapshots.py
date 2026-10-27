"""Persist calculated subject valuation and KPI snapshots."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261107_calculation_snapshots"
down_revision: Union[str, None] = "20261106_manual_property_valuations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    import os

    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "calculation_snapshot",
        sa.Column("scope_type", sa.String(24), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("viewer_key", sa.Text(), nullable=False),
        sa.Column("calculation_type", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("algorithm_version", sa.String(40), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "scope_type IN ('document', 'request')", name="ck_calculation_snapshot_scope_type"
        ),
        sa.PrimaryKeyConstraint(
            "scope_type", "scope_id", "viewer_key", "calculation_type", name="pk_calculation_snapshot"
        ),
        schema=schema,
    )
    op.create_index(
        "ix_calculation_snapshot_calculated_at",
        "calculation_snapshot",
        ["calculated_at"],
        schema=schema,
    )


def downgrade() -> None:
    op.drop_table("calculation_snapshot", schema=_schema())
