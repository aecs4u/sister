"""Persist user-edited enfiteusi affrancazione amounts."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261108_property_affrancation_amounts"
down_revision: Union[str, None] = "20261107_calculation_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    import os

    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "property_affrancation_amount",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("asset_key", sa.String(64), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("amount >= 0", name="ck_property_affrancation_amount_nonnegative"),
        sa.PrimaryKeyConstraint("user_id", "asset_key", name="pk_property_affrancation_amount"),
        schema=schema,
    )
    op.create_index(
        "ix_property_affrancation_amount_asset",
        "property_affrancation_amount",
        ["asset_key"],
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_table("property_affrancation_amount", schema=schema)
