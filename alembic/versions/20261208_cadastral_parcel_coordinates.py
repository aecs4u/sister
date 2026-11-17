"""Persist parcel coordinates and their cadastral or ANNCSU source."""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261208_cadastral_parcel_coordinates"
down_revision: Union[str, None] = "20261207_mortgage_charge_allocation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "cadastral_parcel_coordinate",
        sa.Column("cadastral_code", sa.Text(), nullable=False),
        sa.Column("section", sa.Text(), server_default="", nullable=False),
        sa.Column("sheet", sa.Text(), nullable=False),
        sa.Column("parcel", sa.Text(), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("location_source", sa.Text(), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=True),
        sa.Column("source_method", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_cadastral_parcel_coordinate_latitude"),
        sa.CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_cadastral_parcel_coordinate_longitude"),
        sa.CheckConstraint(
            "location_source IN ('cadastral_geometry', 'anncsu_address', 'osm_address')",
            name="ck_cadastral_parcel_coordinate_source",
        ),
        sa.PrimaryKeyConstraint(
            "cadastral_code", "section", "sheet", "parcel", name="pk_cadastral_parcel_coordinate"
        ),
        schema=schema,
    )
    op.create_index(
        "ix_cadastral_parcel_coordinate_source",
        "cadastral_parcel_coordinate",
        ["location_source"],
        schema=schema,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cadastral_parcel_coordinate_source",
        table_name="cadastral_parcel_coordinate",
        schema=_schema(),
    )
    op.drop_table("cadastral_parcel_coordinate", schema=_schema())
