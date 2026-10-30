"""Give document purchases a source, price, payment reference, expiry and a soft-revocation trail.

Purchases stay the access entitlement; ``aecs4u-billing`` (Stripe checkout / wallet) fills ``source``,
``amount_cents``, ``payment_ref`` and ``wallet_tx_id`` when it fulfils an order, admins record
``source='admin_grant'``. Revoking keeps the row (``revoked_at``) instead of deleting the history.
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261118_document_purchase_billing"
down_revision: Union[str, None] = "20261117_subject_notes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None

_COLUMNS = (
    ("source", sa.Column("source", sa.String(length=32), nullable=False, server_default="admin_grant")),
    ("amount_cents", sa.Column("amount_cents", sa.Integer(), nullable=True)),
    ("currency", sa.Column("currency", sa.String(length=3), nullable=True)),
    ("payment_ref", sa.Column("payment_ref", sa.String(), nullable=True)),
    ("wallet_tx_id", sa.Column("wallet_tx_id", sa.String(), nullable=True)),
    ("expires_at", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)),
    ("revoked_at", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True)),
    ("revoked_by", sa.Column("revoked_by", sa.String(), nullable=True)),
    ("revoked_reason", sa.Column("revoked_reason", sa.Text(), nullable=True)),
)


def upgrade() -> None:
    schema = op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("document_purchase", schema=schema)}
    for name, column in _COLUMNS:
        if name not in existing:
            op.add_column("document_purchase", column, schema=schema)
    indexes = {index["name"] for index in inspector.get_indexes("document_purchase", schema=schema)}
    if "ux_document_purchase_payment_ref" not in indexes:
        op.create_index(
            "ux_document_purchase_payment_ref",
            "document_purchase",
            ["payment_ref"],
            unique=True,
            schema=schema,
            postgresql_where=sa.text("payment_ref IS NOT NULL"),
        )


def downgrade() -> None:
    schema = op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")
    op.drop_index("ux_document_purchase_payment_ref", table_name="document_purchase", schema=schema)
    for name, _ in reversed(_COLUMNS):
        op.drop_column("document_purchase", name, schema=schema)
