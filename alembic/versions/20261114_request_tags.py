"""Add normalized source tags to persisted requests."""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261114_request_tags"
down_revision: Union[str, None] = "20261113_cadastral_geometry_views"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("request", schema=schema):
        return

    if not inspector.has_table("request_tag", schema=schema):
        op.create_table(
            "request_tag",
            sa.Column("request_id", sa.BigInteger(), nullable=False),
            sa.Column("tag", sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(
                ["request_id"],
                [f"{schema}.request.id"],
                name="request_tag_request_id_fkey",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("request_id", "tag", name="request_tag_pkey"),
            schema=schema,
        )

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("request_tag", schema=schema)}
    if "ix_request_tag_tag" not in indexes:
        op.create_index("ix_request_tag_tag", "request_tag", ["tag"], schema=schema)


def downgrade() -> None:
    schema = _schema()
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("request_tag", schema=schema):
        indexes = {index["name"] for index in inspector.get_indexes("request_tag", schema=schema)}
        if "ix_request_tag_tag" in indexes:
            op.drop_index("ix_request_tag_tag", table_name="request_tag", schema=schema)
        op.drop_table("request_tag", schema=schema)
