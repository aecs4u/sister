"""Tell apart the registry records that share a fiscal code (denominazione + sede).

SISTER lists a codice fiscale / partita IVA under several records; ``subject`` is unique per code, so their properties
were mixed. ``subject_record`` holds one row per (code, denominazione, sede); ``property`` and ``result_owner`` point at
the record they were found under. Additive only: existing rows keep ``subject_record_id`` NULL until
``sister db backfill`` projects them again.
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261123_subject_record"
down_revision: Union[str, None] = "20261122_office_listing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def _columns(inspector, table: str, schema: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table, schema=schema)}


def upgrade() -> None:
    schema = _schema()
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("subject_record", schema=schema):
        op.create_table(
            "subject_record",
            sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
            sa.Column("subject_id", sa.BigInteger(), nullable=True),
            sa.Column("fiscal_code", sa.String(), nullable=False),
            sa.Column("denomination", sa.String(), nullable=False),
            sa.Column("seat", sa.String(), server_default="", nullable=False),
            sa.Column("seat_municipality", sa.String(), nullable=True),
            sa.Column("seat_province_code", sa.String(length=2), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["subject_id"], [f"{schema}.subject.id"], name="fk_subject_record_subject"),
            sa.PrimaryKeyConstraint("id", name="pk_subject_record"),
            sa.UniqueConstraint("fiscal_code", "denomination", "seat", name="uq_subject_record_identity"),
            schema=schema,
        )
        op.create_index("ix_subject_record_subject", "subject_record", ["subject_id"], schema=schema)
        op.create_index("ix_subject_record_fiscal_code", "subject_record", ["fiscal_code"], schema=schema)
    for table in ("property", "result_owner"):
        if "subject_record_id" not in _columns(inspector, table, schema):
            op.add_column(table, sa.Column("subject_record_id", sa.BigInteger(), nullable=True), schema=schema)
            op.create_foreign_key(
                f"fk_{table}_subject_record", table, "subject_record", ["subject_record_id"], ["id"],
                source_schema=schema, referent_schema=schema,
            )
            op.create_index(f"ix_{table}_subject_record", table, ["subject_record_id"], schema=schema)
    op.execute(
        f"""CREATE OR REPLACE VIEW {schema}.v_subject_record_property AS
        SELECT r.id AS subject_record_id, r.fiscal_code, r.denomination, r.seat, r.seat_municipality,
               r.seat_province_code, p.id AS property_id, p.request_id, p.property_type, p.cadastral_unit_id,
               p.address, p.category, p.area_sqm, p.income_eur
        FROM {schema}.subject_record r
        LEFT JOIN {schema}.property p ON p.subject_record_id = r.id"""
    )


def downgrade() -> None:
    schema = _schema()
    op.execute(f"DROP VIEW IF EXISTS {schema}.v_subject_record_property")
    for table in ("result_owner", "property"):
        op.drop_index(f"ix_{table}_subject_record", table_name=table, schema=schema)
        op.drop_constraint(f"fk_{table}_subject_record", table, schema=schema, type_="foreignkey")
        op.drop_column(table, "subject_record_id", schema=schema)
    op.drop_index("ix_subject_record_fiscal_code", table_name="subject_record", schema=schema)
    op.drop_index("ix_subject_record_subject", table_name="subject_record", schema=schema)
    op.drop_table("subject_record", schema=schema)
