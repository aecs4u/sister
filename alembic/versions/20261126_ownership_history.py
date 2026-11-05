"""Ownership and property-state history: one row per period, appended only when something changes.

Every saved search used to add a full copy of what it found, identical or not, and nothing said *when* an owner or a
classamento changed. ``ownership_history`` keeps one row per period in which a subject held a right on a cadastral unit
(first/last seen, the searches that saw it, when it stopped being listed); ``property_state_history`` does the same for
the unit's classamento/consistenza/rendita. An identical answer only extends the open period.

``request.content_hash`` identifies an answer's content; ``request.same_as_request_id`` points a repeated identical
answer at the earlier one that holds the content (payload and rows are not stored twice). Additive only.
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261126_ownership_history"
down_revision: Union[str, None] = "20261125_holding_details"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None

_PERIOD = (
    ("first_seen_at", sa.DateTime(timezone=True), False),
    ("first_request_id", sa.BigInteger(), False),
    ("last_seen_at", sa.DateTime(timezone=True), False),
    ("last_request_id", sa.BigInteger(), False),
    ("seen_count", sa.Integer(), False),
    ("ended_at", sa.DateTime(timezone=True), True),
    ("ended_request_id", sa.BigInteger(), True),
)


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def _period_columns(schema: str) -> list:
    columns = []
    for name, kind, nullable in _PERIOD:
        default = sa.text("1") if name == "seen_count" else None
        columns.append(sa.Column(name, kind, nullable=nullable, server_default=default))
    return columns


def _period_fks(table: str, schema: str) -> list:
    return [
        sa.ForeignKeyConstraint([column], [f"{schema}.request.id"], name=f"fk_{table}_{column}")
        for column in ("first_request_id", "last_request_id", "ended_request_id")
    ]


def upgrade() -> None:
    schema = _schema()
    inspector = sa.inspect(op.get_bind())
    request_columns = {c["name"] for c in inspector.get_columns("request", schema=schema)}
    if "content_hash" not in request_columns:
        op.add_column("request", sa.Column("content_hash", sa.String(), nullable=True), schema=schema)
        op.create_index("ix_request_content_hash", "request", ["content_hash"], schema=schema)
    if "same_as_request_id" not in request_columns:
        op.add_column("request", sa.Column("same_as_request_id", sa.BigInteger(), nullable=True), schema=schema)
        op.create_foreign_key(
            "fk_request_same_as", "request", "request", ["same_as_request_id"], ["id"], source_schema=schema, referent_schema=schema
        )
        op.create_index("ix_request_same_as", "request", ["same_as_request_id"], schema=schema)

    if not inspector.has_table("ownership_history", schema=schema):
        op.create_table(
            "ownership_history",
            sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
            sa.Column("cadastral_unit_id", sa.BigInteger(), nullable=False),
            sa.Column("subject_id", sa.BigInteger(), nullable=False),
            sa.Column("subject_record_id", sa.BigInteger(), nullable=True),
            sa.Column("right_id", sa.BigInteger(), nullable=True),
            *_period_columns(schema),
            sa.PrimaryKeyConstraint("id", name="pk_ownership_history"),
            sa.ForeignKeyConstraint(["cadastral_unit_id"], [f"{schema}.cadastral_unit.id"], name="fk_ownership_history_unit"),
            sa.ForeignKeyConstraint(["subject_id"], [f"{schema}.subject.id"], name="fk_ownership_history_subject"),
            sa.ForeignKeyConstraint(["subject_record_id"], [f"{schema}.subject_record.id"], name="fk_ownership_history_record"),
            sa.ForeignKeyConstraint(["right_id"], [f"{schema}.ownership_right.id"], name="fk_ownership_history_right"),
            *_period_fks("ownership_history", schema),
            schema=schema,
        )
        op.create_index("ix_ownership_history_unit", "ownership_history", ["cadastral_unit_id"], schema=schema)
        op.create_index("ix_ownership_history_subject", "ownership_history", ["subject_id"], schema=schema)
        op.create_index("ix_ownership_history_last_request", "ownership_history", ["last_request_id"], schema=schema)
        # at most one open period per (unit, subject, right, record)
        op.execute(
            f"CREATE UNIQUE INDEX uq_ownership_history_open ON {schema}.ownership_history "
            "(cadastral_unit_id, subject_id, right_id, subject_record_id) NULLS NOT DISTINCT WHERE ended_at IS NULL"
        )

    if not inspector.has_table("property_state_history", schema=schema):
        op.create_table(
            "property_state_history",
            sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
            sa.Column("cadastral_unit_id", sa.BigInteger(), nullable=False),
            sa.Column("state_hash", sa.String(), nullable=False),
            sa.Column("property_type", sa.String(), nullable=True),
            sa.Column("category", sa.String(), nullable=True),
            sa.Column("cadastral_class", sa.String(), nullable=True),
            sa.Column("consistency_value", sa.Numeric(), nullable=True),
            sa.Column("consistency_unit", sa.String(), nullable=True),
            sa.Column("census_zone", sa.String(), nullable=True),
            sa.Column("land_quality", sa.String(), nullable=True),
            sa.Column("area_sqm", sa.Numeric(), nullable=True),
            sa.Column("income_eur", sa.Numeric(14, 2), nullable=True),
            sa.Column("dominical_income_eur", sa.Numeric(14, 2), nullable=True),
            sa.Column("agricultural_income_eur", sa.Numeric(14, 2), nullable=True),
            sa.Column("address", sa.String(), nullable=True),
            sa.Column("partita", sa.String(), nullable=True),
            *_period_columns(schema),
            sa.PrimaryKeyConstraint("id", name="pk_property_state_history"),
            sa.ForeignKeyConstraint(["cadastral_unit_id"], [f"{schema}.cadastral_unit.id"], name="fk_property_state_history_unit"),
            *_period_fks("property_state_history", schema),
            schema=schema,
        )
        op.create_index("ix_property_state_history_unit", "property_state_history", ["cadastral_unit_id"], schema=schema)
        op.create_index("ix_property_state_history_last_request", "property_state_history", ["last_request_id"], schema=schema)
        op.execute(
            f"CREATE UNIQUE INDEX uq_property_state_history_open ON {schema}.property_state_history (cadastral_unit_id) "
            "WHERE ended_at IS NULL"
        )

    op.execute(
        f"""CREATE OR REPLACE VIEW {schema}.v_ownership_history AS
        SELECT h.id, h.cadastral_unit_id, u.cadastre_type, u.comune_id, u.section, u.sheet, u.parcel, u.subunit,
               h.subject_id, s.fiscal_code, s.name AS subject_name, h.subject_record_id, r.denomination AS record_denomination,
               r.seat AS record_seat, h.right_id, k.right_type, k.right_code, k.description AS right_description,
               o.share_numerator, o.share_denominator,
               h.first_seen_at, h.last_seen_at, h.seen_count, h.ended_at, (h.ended_at IS NULL) AS current,
               h.first_request_id, h.last_request_id, h.ended_request_id
        FROM {schema}.ownership_history h
        JOIN {schema}.cadastral_unit u ON u.id = h.cadastral_unit_id
        JOIN {schema}.subject s ON s.id = h.subject_id
        LEFT JOIN {schema}.subject_record r ON r.id = h.subject_record_id
        LEFT JOIN {schema}.ownership_right o ON o.id = h.right_id
        LEFT JOIN {schema}.right_kind k ON k.id = o.right_kind_id"""
    )


def downgrade() -> None:
    schema = _schema()
    op.execute(f"DROP VIEW IF EXISTS {schema}.v_ownership_history")
    op.drop_table("property_state_history", schema=schema)
    op.drop_table("ownership_history", schema=schema)
    op.drop_index("ix_request_same_as", table_name="request", schema=schema)
    op.drop_constraint("fk_request_same_as", "request", schema=schema, type_="foreignkey")
    op.drop_column("request", "same_as_request_id", schema=schema)
    op.drop_index("ix_request_content_hash", table_name="request", schema=schema)
    op.drop_column("request", "content_hash", schema=schema)
