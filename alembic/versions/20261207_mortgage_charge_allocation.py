"""Link attributed mortgage costs to their evidence-backed ledger allocation."""

import os

import sqlalchemy as sa
from alembic import op

revision = "20261207_mortgage_charge_allocation"
down_revision = "20261206_ade_statement_ledger"
branch_labels = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    op.add_column(
        "mortgage_search_charge",
        sa.Column("allocation_id", sa.BigInteger(), nullable=True),
        schema=schema,
    )
    op.create_foreign_key(
        "fk_mortgage_search_charge_allocation_id_ade_charge_allocation",
        "mortgage_search_charge",
        "ade_charge_allocation",
        ["allocation_id"],
        ["id"],
        source_schema=schema,
        referent_schema=schema,
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_mortgage_search_charge_allocation",
        "mortgage_search_charge",
        ["allocation_id"],
        schema=schema,
    )
    op.create_unique_constraint(
        "uq_ade_charge_allocation_receipt_line",
        "ade_charge_allocation",
        ["receipt_line_id"],
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_constraint(
        "uq_ade_charge_allocation_receipt_line", "ade_charge_allocation", schema=schema, type_="unique"
    )
    op.drop_constraint(
        "uq_mortgage_search_charge_allocation", "mortgage_search_charge", schema=schema, type_="unique"
    )
    op.drop_constraint(
        "fk_mortgage_search_charge_allocation_id_ade_charge_allocation",
        "mortgage_search_charge",
        schema=schema,
        type_="foreignkey",
    )
    op.drop_column("mortgage_search_charge", "allocation_id", schema=schema)
