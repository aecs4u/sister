"""Persist what each subject holds (counts, rendite, valori catastali) for the ``/persons`` ranking.

``subject_holding`` has one row per codice fiscale / partita IVA, computed from its latest subject search. It is
derived data: fill it with ``sister db backfill holdings`` after upgrading; new subject searches refresh their row.
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261124_subject_holding"
down_revision: Union[str, None] = "20261123_subject_record"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("subject_holding", schema=schema):
        return
    op.create_table(
        "subject_holding",
        sa.Column("fiscal_code", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=True),
        sa.Column("provinces", sa.String(), nullable=True),
        sa.Column("province_count", sa.Integer(), nullable=True),
        sa.Column("buildings", sa.Integer(), nullable=True),
        sa.Column("land_parcels", sa.Integer(), nullable=True),
        sa.Column("properties", sa.Integer(), nullable=True),
        sa.Column("full_ownership", sa.Integer(), nullable=True),
        sa.Column("unvalued", sa.Integer(), nullable=True),
        sa.Column("cadastral_income_eur", sa.Numeric(16, 2), nullable=True),
        sa.Column("dominical_income_eur", sa.Numeric(16, 2), nullable=True),
        sa.Column("cadastral_value_eur", sa.Numeric(18, 2), nullable=True),
        sa.Column("cadastral_value_share_eur", sa.Numeric(18, 2), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id"], [f"{schema}.request.id"], name="fk_subject_holding_request", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("fiscal_code", name="pk_subject_holding"),
        schema=schema,
    )


def downgrade() -> None:
    op.drop_table("subject_holding", schema=_schema())
