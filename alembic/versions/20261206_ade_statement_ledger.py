"""Persist AdE account-statement, receipt, and attribution evidence separately from app-credit wallets."""

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261206_ade_statement_ledger"
down_revision = "20261205_mortgage_search_tables"
branch_labels = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "ade_statement_import",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("total_credit", sa.Numeric(12, 2), nullable=False),
        sa.Column("total_debit", sa.Numeric(12, 2), nullable=False),
        sa.Column("closing_balance", sa.Numeric(12, 2), nullable=True),
        sa.Column("source_filename", sa.String(512), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column(
            "summary", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("period_end >= period_start", name="ck_ade_statement_import_period"),
        sa.PrimaryKeyConstraint("id", name="pk_ade_statement_import"),
        sa.UniqueConstraint("source_sha256", name="uq_ade_statement_import_sha256"),
        schema=schema,
    )
    op.create_table(
        "ade_statement_row",
        sa.Column("statement_key", sa.String(256), nullable=False),
        sa.Column("import_id", sa.BigInteger(), nullable=False),
        sa.Column("posted_on", sa.Date(), nullable=False),
        sa.Column("office", sa.String(32), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("credit_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("debit_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(3), server_default="EUR", nullable=False),
        sa.Column(
            "row_data", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column("detail_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("detail_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("currency = 'EUR'", name="ck_ade_statement_row_currency"),
        sa.CheckConstraint("credit_amount IS NULL OR credit_amount >= 0", name="ck_ade_statement_row_credit"),
        sa.CheckConstraint("debit_amount IS NULL OR debit_amount >= 0", name="ck_ade_statement_row_debit"),
        sa.CheckConstraint("num_nonnulls(credit_amount, debit_amount) = 1", name="ck_ade_statement_row_one_side"),
        sa.ForeignKeyConstraint(
            ["import_id"], [f"{schema}.ade_statement_import.id"],
            name="fk_ade_statement_row_import_id_ade_statement_import", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("statement_key", name="pk_ade_statement_row"),
        schema=schema,
    )
    op.create_index("ix_ade_statement_row_date", "ade_statement_row", ["posted_on"], schema=schema)

    op.create_table(
        "ade_ledger_entry",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("statement_key", sa.String(256), nullable=False),
        sa.Column("posted_on", sa.Date(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("entry_type", sa.String(16), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("balance_after", sa.Numeric(12, 2), nullable=False),
        sa.Column("pagopa_payment_id", sa.BigInteger(), nullable=True),
        sa.Column("linked_by", sa.String(255), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("entry_type IN ('topup', 'usage')", name="ck_ade_ledger_entry_type"),
        sa.CheckConstraint("amount > 0", name="ck_ade_ledger_entry_amount"),
        sa.ForeignKeyConstraint(
            ["statement_key"], [f"{schema}.ade_statement_row.statement_key"],
            name="fk_ade_ledger_entry_statement_key_ade_statement_row", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pagopa_payment_id"], [f"{schema}.pagopa_payment_history.id"],
            name="fk_ade_ledger_entry_pagopa_payment_id_pagopa_payment_history", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ade_ledger_entry"),
        sa.UniqueConstraint("statement_key", name="uq_ade_ledger_entry_statement_key"),
        sa.UniqueConstraint("pagopa_payment_id", name="uq_ade_ledger_entry_pagopa_payment"),
        schema=schema,
    )
    op.create_index("ix_ade_ledger_entry_date", "ade_ledger_entry", ["posted_on", "sequence_no"], schema=schema)

    op.create_table(
        "ade_receipt",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("receipt_key", sa.String(256), nullable=False),
        sa.Column("receipt_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), server_default="EUR", nullable=False),
        sa.Column("payment_method", sa.String(64), nullable=True),
        sa.Column("source_filename", sa.String(512), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "receipt_data", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("amount >= 0", name="ck_ade_receipt_amount"),
        sa.CheckConstraint("currency = 'EUR'", name="ck_ade_receipt_currency"),
        sa.ForeignKeyConstraint(["document_id"], [f"{schema}.document.id"], name="fk_ade_receipt_document"),
        sa.PrimaryKeyConstraint("id", name="pk_ade_receipt"),
        sa.UniqueConstraint("receipt_key", name="uq_ade_receipt_key"),
        sa.UniqueConstraint("source_sha256", name="uq_ade_receipt_sha256"),
        schema=schema,
    )
    op.create_table(
        "ade_receipt_line",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("receipt_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("element", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), server_default="1", nullable=False),
        sa.Column("unit_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.CheckConstraint("quantity > 0 AND unit_amount >= 0 AND amount >= 0", name="ck_ade_receipt_line_amounts"),
        sa.ForeignKeyConstraint(
            ["receipt_id"], [f"{schema}.ade_receipt.id"], name="fk_ade_receipt_line_receipt_id_ade_receipt", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ade_receipt_line"),
        sa.UniqueConstraint("receipt_id", "ordinal", name="uq_ade_receipt_line_ordinal"),
        schema=schema,
    )
    op.create_table(
        "ade_charge_allocation",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("statement_key", sa.String(256), nullable=False),
        sa.Column("search_id", sa.BigInteger(), nullable=True),
        sa.Column("receipt_line_id", sa.BigInteger(), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("evidence", sa.String(32), nullable=False),
        sa.Column("evidence_ref", sa.String(256), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_ade_charge_allocation_amount"),
        sa.CheckConstraint(
            "evidence IN ('statement_detail', 'receipt', 'single_inspection_amount', 'unattributed')",
            name="ck_ade_charge_allocation_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["statement_key"], [f"{schema}.ade_statement_row.statement_key"],
            name="fk_ade_charge_allocation_statement_key_ade_statement_row", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["search_id"], [f"{schema}.mortgage_search.id"],
            name="fk_ade_charge_allocation_search_id_mortgage_search", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["receipt_line_id"], [f"{schema}.ade_receipt_line.id"],
            name="fk_ade_charge_allocation_receipt_line_id_ade_receipt_line", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ade_charge_allocation"),
        sa.UniqueConstraint("statement_key", "evidence_ref", name="uq_ade_charge_allocation_evidence"),
        schema=schema,
    )
    op.create_index("ix_ade_charge_allocation_search", "ade_charge_allocation", ["search_id"], schema=schema)


def downgrade() -> None:
    schema = _schema()
    op.drop_index("ix_ade_ledger_entry_date", table_name="ade_ledger_entry", schema=schema)
    op.drop_table("ade_ledger_entry", schema=schema)
    op.drop_index("ix_ade_charge_allocation_search", table_name="ade_charge_allocation", schema=schema)
    op.drop_table("ade_charge_allocation", schema=schema)
    op.drop_table("ade_receipt_line", schema=schema)
    op.drop_table("ade_receipt", schema=schema)
    op.drop_index("ix_ade_statement_row_date", table_name="ade_statement_row", schema=schema)
    op.drop_table("ade_statement_row", schema=schema)
    op.drop_table("ade_statement_import", schema=schema)
