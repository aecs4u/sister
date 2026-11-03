"""Add the portal's "Elenco uffici" listing (provincial offices and their data update date)."""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261122_office_listing"
down_revision: Union[str, None] = "20261121_territory_courts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    if sa.inspect(op.get_bind()).has_table("cadastral_office_listing", schema=schema):
        return
    op.create_table(
        "cadastral_office_listing",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("province_code", sa.String(length=2), nullable=True),
        sa.Column("data_updated_on", sa.Date(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["province_code"], [f"{schema}.territory_province.code"],
            name="fk_cadastral_office_listing_province_code_territory_province", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cadastral_office_listing"),
        sa.UniqueConstraint("name", name="uq_cadastral_office_listing_name"),
        schema=schema,
    )
    op.create_index(
        "ix_cadastral_office_listing_province_code", "cadastral_office_listing", ["province_code"], schema=schema
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_index("ix_cadastral_office_listing_province_code", table_name="cadastral_office_listing", schema=schema)
    op.drop_table("cadastral_office_listing", schema=schema)
