"""Add display metadata and searchable tags to request types."""

from __future__ import annotations

import os
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from alembic import op

revision: str = "20261103_request_type_metadata"
down_revision: Union[str, None] = "20261102_subject_search_outcome"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("request_type", schema=schema):
        return

    columns = {column["name"]: column for column in inspector.get_columns("request_type", schema=schema)}
    for name in ("label", "description"):
        if name not in columns:
            op.add_column("request_type", sa.Column(name, sa.Text(), nullable=True), schema=schema)

    if not inspector.has_table("request_type_tag", schema=schema):
        op.create_table(
            "request_type_tag",
            sa.Column("request_type", sa.Text(), nullable=False),
            sa.Column("tag", sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(
                ["request_type"],
                [f"{schema}.request_type.code"],
                name="request_type_tag_request_type_fkey",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("request_type", "tag", name="request_type_tag_pkey"),
            schema=schema,
        )

    tag_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("request_type_tag", schema=schema)}
    if "ix_request_type_tag_tag" not in tag_indexes:
        op.create_index("ix_request_type_tag_tag", "request_type_tag", ["tag"], schema=schema)

    preparer = bind.dialect.identifier_preparer
    request_table = f"{preparer.quote_schema(schema)}.{preparer.quote('request_type')}"
    codes = bind.execute(sa.text(f"SELECT code FROM {request_table}")).scalars().all()

    from sister.request_types import request_type_info

    for code in codes:
        info = request_type_info(code)
        bind.execute(
            sa.text(f"UPDATE {request_table} SET label = :label, description = :description WHERE code = :code"),
            {"code": code, "label": info.label, "description": info.description},
        )
        bind.execute(
            insert(sa.table("request_type_tag", sa.column("request_type"), sa.column("tag"), schema=schema))
            .values([{"request_type": code, "tag": tag} for tag in info.tags])
            .on_conflict_do_nothing(index_elements=["request_type", "tag"])
        )

    for name in ("label", "description"):
        current = {column["name"]: column for column in sa.inspect(bind).get_columns("request_type", schema=schema)}
        if current[name]["nullable"]:
            op.alter_column("request_type", name, schema=schema, existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    schema = _schema()
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("request_type_tag", schema=schema):
        indexes = {index["name"] for index in inspector.get_indexes("request_type_tag", schema=schema)}
        if "ix_request_type_tag_tag" in indexes:
            op.drop_index("ix_request_type_tag_tag", table_name="request_type_tag", schema=schema)
        op.drop_table("request_type_tag", schema=schema)

    if inspector.has_table("request_type", schema=schema):
        columns = {column["name"] for column in inspector.get_columns("request_type", schema=schema)}
        for name in ("description", "label"):
            if name in columns:
                op.drop_column("request_type", name, schema=schema)
