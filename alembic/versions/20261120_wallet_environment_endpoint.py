"""Add OpenData-compatible environment and endpoint fields to shared wallets."""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261120_wallet_env_endpoint"
down_revision: Union[str, None] = "20261119_billing_wallet_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv(
        "DATABASE_SCHEMA", "sister"
    )


def upgrade() -> None:
    schema = _schema()
    inspector = sa.inspect(op.get_bind())
    wallet_columns = {
        column["name"]
        for column in inspector.get_columns("billing_wallets", schema=schema)
    }
    if "environment" not in wallet_columns:
        op.add_column(
            "billing_wallets",
            sa.Column(
                "environment",
                sa.String(),
                nullable=False,
                server_default="production",
            ),
            schema=schema,
        )
    wallet_indexes = {
        index["name"]
        for index in inspector.get_indexes("billing_wallets", schema=schema)
    }
    if "ix_billing_wallets_environment" not in wallet_indexes:
        op.create_index(
            "ix_billing_wallets_environment",
            "billing_wallets",
            ["environment"],
            schema=schema,
        )

    transaction_columns = {
        column["name"]
        for column in inspector.get_columns("billing_transactions", schema=schema)
    }
    if "endpoint" not in transaction_columns:
        op.add_column(
            "billing_transactions",
            sa.Column("endpoint", sa.String(), nullable=True),
            schema=schema,
        )
    transaction_indexes = {
        index["name"]
        for index in inspector.get_indexes("billing_transactions", schema=schema)
    }
    if "ix_billing_transactions_endpoint" not in transaction_indexes:
        op.create_index(
            "ix_billing_transactions_endpoint",
            "billing_transactions",
            ["endpoint"],
            schema=schema,
        )


def downgrade() -> None:
    schema = _schema()
    inspector = sa.inspect(op.get_bind())
    transaction_columns = {
        column["name"]
        for column in inspector.get_columns("billing_transactions", schema=schema)
    }
    transaction_indexes = {
        index["name"]
        for index in inspector.get_indexes("billing_transactions", schema=schema)
    }
    if "ix_billing_transactions_endpoint" in transaction_indexes:
        op.drop_index(
            "ix_billing_transactions_endpoint",
            table_name="billing_transactions",
            schema=schema,
        )
    if "endpoint" in transaction_columns:
        op.drop_column("billing_transactions", "endpoint", schema=schema)

    wallet_columns = {
        column["name"]
        for column in inspector.get_columns("billing_wallets", schema=schema)
    }
    wallet_indexes = {
        index["name"]
        for index in inspector.get_indexes("billing_wallets", schema=schema)
    }
    if "ix_billing_wallets_environment" in wallet_indexes:
        op.drop_index(
            "ix_billing_wallets_environment",
            table_name="billing_wallets",
            schema=schema,
        )
    if "environment" in wallet_columns:
        op.drop_column("billing_wallets", "environment", schema=schema)
