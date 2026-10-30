"""Add explicit per-user document purchase ownership."""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261116_document_purchases"
down_revision: Union[str, None] = "20261115_subject_favorites"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("document_purchase", schema=schema):
        op.create_table(
            "document_purchase",
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("document_id", sa.BigInteger(), nullable=False),
            sa.Column("purchased_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("created_by", sa.String(), server_default="", nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["document_id"],
                [f"{schema}.document.id"],
                name="fk_document_purchase_document_id_document",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("user_id", "document_id", name="pk_document_purchase"),
            schema=schema,
        )

    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("document_purchase", schema=schema)}
    if "ix_document_purchase_document_id" not in indexes:
        op.create_index(
            "ix_document_purchase_document_id",
            "document_purchase",
            ["document_id"],
            schema=schema,
        )


def downgrade() -> None:
    schema = op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")
    op.drop_table("document_purchase", schema=schema)
