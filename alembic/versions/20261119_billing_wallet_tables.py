"""Tables of the aecs4u-billing SQL wallet (prepaid credits for document purchases), in SISTER's schema.

The definitions come from the package itself (``aecs4u_billing.wallet.sqlmodel_storage``) so they cannot drift from what
``SQLModelWalletStorage`` reads and writes.
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261119_billing_wallet_tables"
down_revision: Union[str, None] = "20261118_document_purchase_billing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    from aecs4u_billing.wallet.sqlmodel_storage import TransactionDB, WalletDB

    schema = _schema()
    metadata = sa.MetaData(schema=schema)
    for model in (WalletDB, TransactionDB):  # wallets first: transactions reference them
        model.__table__.to_metadata(metadata)
    metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    schema = _schema()
    op.drop_table("billing_transactions", schema=schema)
    op.drop_table("billing_wallets", schema=schema)
