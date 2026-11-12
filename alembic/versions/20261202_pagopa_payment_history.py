"""Keep imported pagoPA attempts in a separate, balance-neutral history table."""

import os

import sqlalchemy as sa

from alembic import op

revision = "20261202_pagopa_payment_history"
down_revision = "20261201_billing_invoice_tables"
branch_labels = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    from sister.db_models import PagoPAPaymentHistory

    metadata = sa.MetaData(schema=_schema())
    PagoPAPaymentHistory.__table__.to_metadata(metadata)
    metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    schema = _schema()
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("pagopa_payment_history", schema=schema):
        op.drop_table("pagopa_payment_history", schema=schema)
