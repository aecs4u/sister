"""Create the canonical aecs4u-billing invoice tables in SISTER's schema."""

import os

import sqlalchemy as sa

from alembic import op

revision = "20261201_billing_invoice_tables"
down_revision = "20261130_wallet_gift_coupons"
branch_labels = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    from aecs4u_billing.invoicing.sqlmodel_storage import InvoiceDB, InvoiceLineItemDB

    metadata = sa.MetaData(schema=_schema())
    for model in (InvoiceDB, InvoiceLineItemDB):
        model.__table__.to_metadata(metadata)
    metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    schema = _schema()
    inspector = sa.inspect(op.get_bind())
    for table_name in ("billing_invoice_line_items", "billing_invoices"):
        if inspector.has_table(table_name, schema=schema):
            op.drop_table(table_name, schema=schema)
