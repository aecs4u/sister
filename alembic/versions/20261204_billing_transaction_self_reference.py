"""Enforce wallet transaction-to-transaction references in the SQL backend."""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261204_billing_transaction_self_reference"
down_revision: Union[str, None] = "20261203_schema_identity_flatness"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None

CONSTRAINT_NAME = "fk_billing_transactions_related_transaction"


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    bind = op.get_bind()
    schema = _schema()
    preparer = bind.dialect.identifier_preparer
    qualified_table = f"{preparer.quote_schema(schema)}.{preparer.quote('billing_transactions')}"

    orphan_count = bind.execute(
        sa.text(
            f"SELECT count(*) FROM {qualified_table} AS child "
            "WHERE child.related_transaction_id IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {qualified_table} AS parent "
            "WHERE parent.id = child.related_transaction_id)"
        )
    ).scalar_one()
    if orphan_count:
        raise RuntimeError(
            "Cannot add billing transaction self-reference: "
            f"{orphan_count} related_transaction_id value(s) have no billing_transactions.id parent."
        )

    op.execute(
        sa.text(
            f"ALTER TABLE {qualified_table} ADD CONSTRAINT "
            f"{preparer.quote(CONSTRAINT_NAME)} "
            f"FOREIGN KEY (related_transaction_id) REFERENCES {qualified_table} (id) "
            "ON DELETE RESTRICT NOT VALID"
        )
    )
    op.execute(
        sa.text(
            f"ALTER TABLE {qualified_table} VALIDATE CONSTRAINT {preparer.quote(CONSTRAINT_NAME)}"
        )
    )


def downgrade() -> None:
    op.drop_constraint(
        CONSTRAINT_NAME,
        "billing_transactions",
        schema=_schema(),
        type_="foreignkey",
    )
