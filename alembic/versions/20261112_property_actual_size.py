"""Persist each user's effective cadastral property size for OMI valuation."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261112_property_actual_size"
down_revision: Union[str, None] = "20261111_consistency_measure_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    import os

    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "property_actual_size",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("asset_key", sa.String(64), nullable=False),
        sa.Column("actual_size_sqm", sa.Numeric(12, 2), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("actual_size_sqm > 0", name="ck_property_actual_size_positive"),
        sa.PrimaryKeyConstraint("user_id", "asset_key", name="pk_property_actual_size"),
        schema=schema,
    )
    op.create_index(
        "ix_property_actual_size_asset",
        "property_actual_size",
        ["asset_key"],
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_index("ix_property_actual_size_asset", table_name="property_actual_size", schema=schema)
    op.drop_table("property_actual_size", schema=schema)
