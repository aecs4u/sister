"""Track wallet gift coupons and their redemptions."""

import os

import sqlalchemy as sa

from alembic import op

revision = "20261130_wallet_gift_coupons"
down_revision = "20261129_subject_purchases"
branch_labels = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("wallet_gift_coupon", schema=schema):
        op.create_table(
            "wallet_gift_coupon",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("code_hash", sa.String(64), nullable=False),
            sa.Column("code_hint", sa.String(8), nullable=False),
            sa.Column("amount", sa.Numeric(12, 2), nullable=False),
            sa.Column("max_redemptions", sa.Integer(), nullable=False),
            sa.Column("redeemed_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("status", sa.String(16), server_default="active", nullable=False),
            sa.Column("created_by", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.CheckConstraint("amount > 0", name="ck_wallet_gift_coupon_amount"),
            sa.CheckConstraint("max_redemptions > 0", name="ck_wallet_gift_coupon_max_redemptions"),
            sa.CheckConstraint("redeemed_count >= 0 AND redeemed_count <= max_redemptions", name="ck_wallet_gift_coupon_redemptions"),
            sa.CheckConstraint("status IN ('active', 'frozen', 'revoked')", name="ck_wallet_gift_coupon_status"),
            sa.PrimaryKeyConstraint("id", name="pk_wallet_gift_coupon"),
            sa.UniqueConstraint("code_hash", name="uq_wallet_gift_coupon_code_hash"),
            schema=schema,
        )
    if not inspector.has_table("wallet_gift_coupon_redemption", schema=schema):
        op.create_table(
            "wallet_gift_coupon_redemption",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("coupon_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("amount", sa.Numeric(12, 2), nullable=False),
            sa.Column("status", sa.String(24), server_default="pending", nullable=False),
            sa.Column("wallet_tx_id", sa.String(), nullable=True),
            sa.Column("reversed_tx_id", sa.String(), nullable=True),
            sa.Column("redeemed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.CheckConstraint("status IN ('pending', 'completed', 'reversed', 'reversal_required')", name="ck_wallet_gift_coupon_redemption_status"),
            sa.ForeignKeyConstraint(["coupon_id"], [f"{schema}.wallet_gift_coupon.id"], name="fk_wallet_gift_coupon_redemption_coupon_id_wallet_gift_coupon", ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id", name="pk_wallet_gift_coupon_redemption"),
            sa.UniqueConstraint("coupon_id", "user_id", name="uq_wallet_gift_coupon_redemption_user"),
            schema=schema,
        )
        op.create_index("ix_wallet_gift_coupon_redemption_coupon_id", "wallet_gift_coupon_redemption", ["coupon_id"], schema=schema)
        op.create_index("ix_wallet_gift_coupon_redemption_user_id", "wallet_gift_coupon_redemption", ["user_id"], schema=schema)


def downgrade() -> None:
    schema = _schema()
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("wallet_gift_coupon_redemption", schema=schema):
        op.drop_table("wallet_gift_coupon_redemption", schema=schema)
    if inspector.has_table("wallet_gift_coupon", schema=schema):
        op.drop_table("wallet_gift_coupon", schema=schema)
