"""Refine labels and category tags for legacy request types."""

from __future__ import annotations

import os
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20261104_request_type_catalog_refinements"
down_revision: Union[str, None] = "20261103_request_type_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    bind = op.get_bind()
    schema = _schema()
    preparer = bind.dialect.identifier_preparer
    request_table = f"{preparer.quote_schema(schema)}.{preparer.quote('request_type')}"
    tag_table = f"{preparer.quote_schema(schema)}.{preparer.quote('request_type_tag')}"
    codes = bind.execute(sa.text(f"SELECT code FROM {request_table}")).scalars().all()

    from sister.request_types import request_type_info

    for code in codes:
        info = request_type_info(code)
        bind.execute(
            sa.text(f"UPDATE {request_table} SET label = :label, description = :description WHERE code = :code"),
            {"code": code, "label": info.label, "description": info.description},
        )
        bind.execute(sa.text(f"DELETE FROM {tag_table} WHERE request_type = :code"), {"code": code})
        for tag in info.tags:
            bind.execute(
                sa.text(
                    f"INSERT INTO {tag_table} (request_type, tag) VALUES (:code, :tag) "
                    "ON CONFLICT (request_type, tag) DO NOTHING"
                ),
                {"code": code, "tag": tag},
            )


def downgrade() -> None:
    """Keep curated labels and tags when reverting this data-only refinement."""
