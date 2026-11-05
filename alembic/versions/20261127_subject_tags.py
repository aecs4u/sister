"""Shared tags that group person and company profiles.

One row per (subject kind, code, tag). ``tag_key`` is the casefolded tag (the identity), ``tag`` the spelling shown.
Additive only: nothing else changes.
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261127_subject_tags"
down_revision: Union[str, None] = "20261126_ownership_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    if sa.inspect(op.get_bind()).has_table("subject_tag", schema=schema):
        return
    op.create_table(
        "subject_tag",
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("subject_code", sa.String(length=16), nullable=False),
        sa.Column("tag_key", sa.String(length=40), nullable=False),
        sa.Column("tag", sa.String(length=40), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("subject_type IN ('company', 'person')", name="ck_subject_tag_type"),
        sa.PrimaryKeyConstraint("subject_type", "subject_code", "tag_key", name="pk_subject_tag"),
        schema=schema,
    )
    op.create_index("ix_subject_tag_type_key", "subject_tag", ["subject_type", "tag_key"], schema=schema)


def downgrade() -> None:
    schema = _schema()
    op.drop_index("ix_subject_tag_type_key", table_name="subject_tag", schema=schema)
    op.drop_table("subject_tag", schema=schema)
