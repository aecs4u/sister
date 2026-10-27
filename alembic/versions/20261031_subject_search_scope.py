"""Persist the search scope of Visura per Soggetto documents."""

from __future__ import annotations

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261031_subject_search_scope"
down_revision: Union[str, None] = "20261030_request_jobs_fk"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def upgrade() -> None:
    from sister.document_scope import subject_search_scope
    from sister.xml_ingest import parse_visura_xml

    bind = op.get_bind()
    schema = os.getenv("DATABASE_SCHEMA", "sister")
    archive_schema = os.getenv("DATABASE_ARCHIVE_SCHEMA", "sister_archive")
    preparer = bind.dialect.identifier_preparer
    document_table = f"{preparer.quote_schema(schema)}.{preparer.quote('document')}"
    content_table = f"{preparer.quote_schema(archive_schema)}.{preparer.quote('document_content')}"
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("document", schema=schema)}

    if "subject_search_scope" not in columns:
        op.add_column("document", sa.Column("subject_search_scope", sa.String(), nullable=True), schema=schema)
    if "subject_search_province" not in columns:
        op.add_column("document", sa.Column("subject_search_province", sa.String(), nullable=True), schema=schema)

    rows = bind.execute(
        sa.text(
            f"SELECT d.id, d.file_name, c.content FROM {document_table} d "
            f"LEFT JOIN {content_table} c ON c.document_id = d.id "
            "WHERE d.document_type = :subject_type OR lower(substr(d.file_name, 1, 3)) = :subject_prefix"
        ),
        {"subject_type": "visura_soggetto", "subject_prefix": "vs_"},
    ).mappings()
    updates = []
    for row in rows:
        parsed = parse_visura_xml(row["content"]) if row["content"] else None
        request_province = ((parsed or {}).get("request") or {}).get("Provincia")
        scope = subject_search_scope(row["file_name"], request_province=request_province)
        if scope["search_scope"] in {"nationwide", "provincial"}:
            updates.append(
                {
                    "id": row["id"],
                    "scope": scope["search_scope"],
                    "province": scope["search_province"] or None,
                }
            )

    if updates:
        bind.execute(
            sa.text(
                f"UPDATE {document_table} SET subject_search_scope = :scope, "
                "subject_search_province = :province WHERE id = :id"
            ),
            updates,
        )


def downgrade() -> None:
    schema = os.getenv("DATABASE_SCHEMA", "sister")
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("document", schema=schema)}
    if "subject_search_province" in columns:
        op.drop_column("document", "subject_search_province", schema=schema)
    if "subject_search_scope" in columns:
        op.drop_column("document", "subject_search_scope", schema=schema)
