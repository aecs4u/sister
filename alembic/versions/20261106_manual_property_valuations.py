"""Store user-entered property valuations and per-property source preferences."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261106_manual_property_valuations"
down_revision: Union[str, None] = "20261105_subject_search_outcome_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    import os

    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "manual_property_valuation",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("asset_key", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("value_min", sa.Numeric(14, 2), nullable=False),
        sa.Column("value_max", sa.Numeric(14, 2), nullable=True),
        sa.Column("source_date", sa.Date(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "provider IN ('immobiliare', 'idealista', 'other')",
            name="ck_manual_property_valuation_provider",
        ),
        sa.CheckConstraint(
            "value_min > 0 AND (value_max IS NULL OR value_max >= value_min)",
            name="ck_manual_property_valuation_range",
        ),
        sa.PrimaryKeyConstraint("user_id", "asset_key", "provider", name="pk_manual_property_valuation"),
        schema=schema,
    )
    op.create_index(
        "ix_manual_property_valuation_asset",
        "manual_property_valuation",
        ["asset_key"],
        schema=schema,
    )
    op.create_table(
        "property_valuation_preference",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("asset_key", sa.String(64), nullable=False),
        sa.Column("preferred_source", sa.String(24), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "asset_key", name="pk_property_valuation_preference"),
        schema=schema,
    )
    op.create_index(
        "ix_property_valuation_preference_asset",
        "property_valuation_preference",
        ["asset_key"],
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_table("property_valuation_preference", schema=schema)
    op.drop_table("manual_property_valuation", schema=schema)
