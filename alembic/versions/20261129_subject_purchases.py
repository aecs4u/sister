"""Record paid purchases of person and company profile access."""

import os

import sqlalchemy as sa

from alembic import op

revision = "20261129_subject_purchases"
down_revision = "20261128_subject_access"
branch_labels = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    if sa.inspect(op.get_bind()).has_table("subject_purchase", schema=schema):
        return
    op.create_table(
        "subject_purchase",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("subject_type", sa.String(16), nullable=False),
        sa.Column("subject_code", sa.String(16), nullable=False),
        sa.Column("purchased_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(), server_default="stripe", nullable=False),
        sa.Column("source", sa.String(32), server_default="stripe", nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("payment_ref", sa.String(), nullable=True),
        sa.Column("wallet_tx_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(24), server_default="completed", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint("subject_type IN ('company', 'person')", name="ck_subject_purchase_type"),
        sa.CheckConstraint(
            "(subject_type = 'company' AND subject_code ~ '^[0-9]{11}$') OR "
            "(subject_type = 'person' AND subject_code ~ '^[A-Z0-9]{16}$')",
            name="ck_subject_purchase_code",
        ),
        sa.CheckConstraint("status IN ('completed', 'refund_required', 'refunded')", name="ck_subject_purchase_status"),
        sa.PrimaryKeyConstraint("id", name="pk_subject_purchase"),
        schema=schema,
    )
    op.create_index("ix_subject_purchase_user_id", "subject_purchase", ["user_id"], schema=schema)
    op.create_index(
        "uq_subject_purchase_payment_ref",
        "subject_purchase",
        ["payment_ref"],
        unique=True,
        postgresql_where=sa.text("payment_ref IS NOT NULL"),
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    if sa.inspect(op.get_bind()).has_table("subject_purchase", schema=schema):
        op.drop_table("subject_purchase", schema=schema)
