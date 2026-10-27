"""Persist per-user OMI zone and typology choices for cadastral properties."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261110_property_omi_selection"
down_revision: Union[str, None] = "20261109_property_omi_overrides"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    import os

    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "property_omi_selection",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("asset_key", sa.String(64), nullable=False),
        sa.Column("zone_code", sa.String(24), nullable=True),
        sa.Column("typology_code", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "typology_code IS NULL OR typology_code > 0",
            name="ck_property_omi_selection_typology",
        ),
        sa.PrimaryKeyConstraint("user_id", "asset_key", name="pk_property_omi_selection"),
        schema=schema,
    )


def downgrade() -> None:
    op.drop_table("property_omi_selection", schema=_schema())
