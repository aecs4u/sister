"""Persist per-user OMI range refinements for cadastral properties."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261109_property_omi_overrides"
down_revision: Union[str, None] = "20261108_property_affrancation_amounts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    import os

    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "property_omi_override",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("asset_key", sa.String(64), nullable=False),
        sa.Column("value_min", sa.Numeric(14, 2), nullable=False),
        sa.Column("value_max", sa.Numeric(14, 2), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "value_min > 0 AND (value_max IS NULL OR value_max >= value_min)",
            name="ck_property_omi_override_range",
        ),
        sa.PrimaryKeyConstraint("user_id", "asset_key", name="pk_property_omi_override"),
        schema=schema,
    )
    op.create_index(
        "ix_property_omi_override_asset",
        "property_omi_override",
        ["asset_key"],
        schema=schema,
    )


def downgrade() -> None:
    op.drop_table("property_omi_override", schema=_schema())
