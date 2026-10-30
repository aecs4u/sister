"""Store per-user favourites for indexed documents."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261031_document_favorites"
down_revision: Union[str, None] = "20261031_subject_search_scope"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def upgrade() -> None:
    import os

    schema = op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("document_favorite", schema=schema):
        op.create_table(
            "document_favorite",
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("document_id", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["document_id"],
                [f"{schema}.document.id"],
                name="fk_document_favorite_document_id_document",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("user_id", "document_id", name="pk_document_favorite"),
            schema=schema,
        )
        inspector = sa.inspect(op.get_bind())

    indexes = {index["name"] for index in inspector.get_indexes("document_favorite", schema=schema)}
    if "ix_document_favorite_document_id" not in indexes:
        op.create_index(
            "ix_document_favorite_document_id",
            "document_favorite",
            ["document_id"],
            schema=schema,
        )


def downgrade() -> None:
    import os

    schema = op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")
    op.drop_table("document_favorite", schema=schema)
