"""Explicit access to archived subject searches, independently of document purchases."""

import os

import sqlalchemy as sa

from alembic import op

revision = "20261128_subject_access"
down_revision = "20261127_subject_tags"
branch_labels = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    if sa.inspect(op.get_bind()).has_table("subject_access", schema=schema):
        return
    op.create_table(
        "subject_access",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("subject_type", sa.String(16), nullable=False),
        sa.Column("subject_code", sa.String(16), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("subject_type IN ('company', 'person')", name="ck_subject_access_type"),
        sa.CheckConstraint(
            "(subject_type = 'company' AND subject_code ~ '^[0-9]{11}$') OR "
            "(subject_type = 'person' AND subject_code ~ '^[A-Z0-9]{16}$')",
            name="ck_subject_access_code",
        ),
        sa.PrimaryKeyConstraint("user_id", "subject_type", "subject_code", name="pk_subject_access"),
        schema=schema,
    )


def downgrade() -> None:
    op.drop_table("subject_access", schema=_schema())
