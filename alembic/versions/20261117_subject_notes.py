"""Store private notes on person and company profiles."""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261117_subject_notes"
down_revision: Union[str, None] = "20261116_document_purchases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("subject_note", schema=schema):
        return

    op.create_table(
        "subject_note",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("subject_code", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("subject_type IN ('company', 'person')", name="ck_subject_note_type"),
        sa.PrimaryKeyConstraint("user_id", "subject_type", "subject_code", name="pk_subject_note"),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")
    op.drop_table("subject_note", schema=schema)
