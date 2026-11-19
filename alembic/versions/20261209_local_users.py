"""Local sign-in accounts in the database (replaces ``local_users.txt``)."""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261209_local_users"
down_revision: Union[str, None] = "20261208_cadastral_parcel_coordinates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    op.create_table(
        "local_user",
        sa.Column("username_key", sa.String(255), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("role", sa.String(16), server_default="user", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('admin', 'user')", name="ck_local_user_role"),
        sa.CheckConstraint("substr(password_hash, 1, 14) = 'pbkdf2_sha256$'", name="ck_local_user_hashed"),
        sa.PrimaryKeyConstraint("username_key", name="pk_local_user"),
        schema=_schema(),
    )


def downgrade() -> None:
    op.drop_table("local_user", schema=_schema())
