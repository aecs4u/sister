"""Add local territory, court, and Conservatoria reference catalogs."""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert

revision: str = "20261121_territory_courts"
down_revision: Union[str, None] = "20261120_wallet_env_endpoint"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def _has_table(name: str, schema: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name, schema=schema)


def upgrade() -> None:
    schema = _schema()
    if not _has_table("territory_province", schema):
        op.create_table(
            "territory_province",
            sa.Column("code", sa.String(length=2), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.PrimaryKeyConstraint("code", name="pk_territory_province"),
            schema=schema,
        )

    if not _has_table("court", schema):
        op.create_table(
            "court",
            sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
            sa.Column("source_id", sa.String(), nullable=True),
            sa.Column("code", sa.String(), nullable=True),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("court_type", sa.String(), nullable=True),
            sa.Column("province_code", sa.String(length=2), nullable=True),
            sa.Column("municipality_code", sa.Integer(), nullable=True),
            sa.Column("address", sa.String(), nullable=True),
            sa.Column("source_url", sa.String(), nullable=True),
            sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["province_code"], [f"{schema}.territory_province.code"],
                name="fk_court_province_code_territory_province", ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_court"),
            sa.UniqueConstraint("code", name="uq_court_code"),
            sa.UniqueConstraint("source_id", name="uq_court_source_id"),
            schema=schema,
        )
        op.create_index("ix_court_municipality_code", "court", ["municipality_code"], schema=schema)
        op.create_index("ix_court_province_code", "court", ["province_code"], schema=schema)

    if not _has_table("court_municipality", schema):
        op.create_table(
            "court_municipality",
            sa.Column("court_id", sa.Integer(), nullable=False),
            sa.Column("municipality_code", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["court_id"], [f"{schema}.court.id"], name="fk_court_municipality_court_id_court", ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("court_id", "municipality_code", name="pk_court_municipality"),
            schema=schema,
        )
        op.create_index(
            "ix_court_municipality_municipality_code", "court_municipality", ["municipality_code"], schema=schema
        )

    if not _has_table("conservatoria_office", schema):
        op.create_table(
            "conservatoria_office",
            sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
            sa.Column("source_id", sa.String(), nullable=True),
            sa.Column("code", sa.String(), nullable=True),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("province_code", sa.String(length=2), nullable=True),
            sa.Column("municipality_code", sa.Integer(), nullable=True),
            sa.Column("address", sa.String(), nullable=True),
            sa.Column("source_url", sa.String(), nullable=True),
            sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["province_code"], [f"{schema}.territory_province.code"],
                name="fk_conservatoria_office_province_code_territory_province", ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_conservatoria_office"),
            sa.UniqueConstraint("code", name="uq_conservatoria_office_code"),
            sa.UniqueConstraint("source_id", name="uq_conservatoria_office_source_id"),
            schema=schema,
        )
        op.create_index(
            "ix_conservatoria_office_municipality_code", "conservatoria_office", ["municipality_code"], schema=schema
        )
        op.create_index(
            "ix_conservatoria_office_province_code", "conservatoria_office", ["province_code"], schema=schema
        )

    if not _has_table("conservatoria_municipality", schema):
        op.create_table(
            "conservatoria_municipality",
            sa.Column("office_id", sa.Integer(), nullable=False),
            sa.Column("municipality_code", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["office_id"], [f"{schema}.conservatoria_office.id"],
                name="fk_conservatoria_municipality_office_id_conservatoria_office", ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("office_id", "municipality_code", name="pk_conservatoria_municipality"),
            schema=schema,
        )
        op.create_index(
            "ix_conservatoria_municipality_municipality_code",
            "conservatoria_municipality",
            ["municipality_code"],
            schema=schema,
        )

    if not _has_table("conservatoria_section", schema):
        op.create_table(
            "conservatoria_section",
            sa.Column("office_id", sa.Integer(), nullable=False),
            sa.Column("municipality_code", sa.Integer(), nullable=False),
            sa.Column("cadastre_type", sa.String(length=1), nullable=False),
            sa.Column("section_code", sa.String(), nullable=False),
            sa.CheckConstraint("cadastre_type IN ('F', 'T')", name="ck_conservatoria_section_cadastre_type"),
            sa.ForeignKeyConstraint(
                ["office_id", "municipality_code"],
                [f"{schema}.conservatoria_municipality.office_id", f"{schema}.conservatoria_municipality.municipality_code"],
                name="fk_conservatoria_section_office_municipality", ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint(
                "office_id", "municipality_code", "cadastre_type", "section_code",
                name="pk_conservatoria_section",
            ),
            schema=schema,
        )

    # Snapshot the province names already used by SISTER so the territory API
    # reads its province catalog from PostgreSQL. Court and office catalogs are
    # intentionally not seeded from unrelated office-name tables.
    from sister.provinces import PROVINCES

    province_table = sa.table(
        "territory_province",
        sa.column("code", sa.String(length=2)),
        sa.column("name", sa.String()),
        schema=schema,
    )
    op.get_bind().execute(
        pg_insert(province_table)
        .values([{"code": code, "name": name} for code, name in sorted(PROVINCES.items())])
        .on_conflict_do_nothing(index_elements=["code"])
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_table("conservatoria_section", schema=schema)
    op.drop_index("ix_conservatoria_municipality_municipality_code", table_name="conservatoria_municipality", schema=schema)
    op.drop_table("conservatoria_municipality", schema=schema)
    op.drop_index("ix_conservatoria_office_province_code", table_name="conservatoria_office", schema=schema)
    op.drop_index("ix_conservatoria_office_municipality_code", table_name="conservatoria_office", schema=schema)
    op.drop_table("conservatoria_office", schema=schema)
    op.drop_index("ix_court_municipality_municipality_code", table_name="court_municipality", schema=schema)
    op.drop_table("court_municipality", schema=schema)
    op.drop_index("ix_court_province_code", table_name="court", schema=schema)
    op.drop_index("ix_court_municipality_code", table_name="court", schema=schema)
    op.drop_table("court", schema=schema)
    op.drop_table("territory_province", schema=schema)
