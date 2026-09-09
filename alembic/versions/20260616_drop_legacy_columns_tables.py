"""drop legacy Italian columns and obsolete tables

Removes columns that were normalised into cadastral_locations, and drops
old tables superseded by the current ORM models.

Revision ID: 20260616_drop_legacy
Revises: 20260616_merge_heads
Create Date: 2026-06-16
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260616_drop_legacy"
down_revision: Union[str, None] = "20260616_merge_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Old tables no longer in the ORM (all empty)
_OBSOLETE_TABLES = ["immobili", "intestati", "xml_document_metadata"]

# Columns removed from visura_requests (location data moved to cadastral_locations)
_REQUESTS_DROP = ["tipo_catasto", "provincia", "comune", "foglio", "particella", "sezione", "subalterno"]

# Columns removed from visura_properties (location data moved to cadastral_locations)
_PROPERTIES_DROP = ["cadastre_type", "sheet", "parcel", "subunit"]

# Columns removed from visura_documents (split into visura_documents + document_metadata)
_DOCUMENTS_DROP = [
    "oggetto", "richiesta_del",
    "provincia", "comune", "foglio", "particella", "subalterno", "sezione_urbana", "tipo_catasto",
    "intestati_json", "dati_immobile_json", "xml_content",
    "visura_subtype", "situazione_al",
]


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    ).fetchone()
    return bool(row and row[0])


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)


def _index_exists(index_name: str) -> bool:
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT count(*) FROM sqlite_master WHERE type='index' AND name=:n"),
        {"n": index_name},
    ).fetchone()
    return bool(row and row[0])


def upgrade() -> None:
    # ── 1. Drop obsolete tables ───────────────────────────────────────────────
    for table in _OBSOLETE_TABLES:
        if _table_exists(table):
            op.drop_table(table)

    # ── 2. visura_requests: drop old location columns ─────────────────────────
    cols_to_drop = [c for c in _REQUESTS_DROP if _column_exists("visura_requests", c)]
    if cols_to_drop:
        # Drop old indexes that reference these columns before batch rewrite
        for idx in ("idx_requests_lookup", "idx_requests_created_at"):
            if _index_exists(idx):
                op.drop_index(idx, table_name="visura_requests")

        with op.batch_alter_table("visura_requests", recreate="always") as batch_op:
            for col in cols_to_drop:
                batch_op.drop_column(col)
            # Recreate the created_at index (still valid)
            batch_op.create_index("idx_requests_created_at", ["created_at"])

    # ── 3. visura_properties: drop old location columns ───────────────────────
    cols_to_drop = [c for c in _PROPERTIES_DROP if _column_exists("visura_properties", c)]
    if cols_to_drop:
        with op.batch_alter_table("visura_properties", recreate="always") as batch_op:
            for col in cols_to_drop:
                batch_op.drop_column(col)

    # ── 4. visura_documents: drop all legacy columns ──────────────────────────
    cols_to_drop = [c for c in _DOCUMENTS_DROP if _column_exists("visura_documents", c)]
    if cols_to_drop:
        # Drop old lookup index before rewrite
        if _index_exists("idx_documents_lookup"):
            op.drop_index("idx_documents_lookup", table_name="visura_documents")

        with op.batch_alter_table("visura_documents", recreate="always") as batch_op:
            for col in cols_to_drop:
                batch_op.drop_column(col)


def downgrade() -> None:
    # visura_documents: restore legacy columns
    with op.batch_alter_table("visura_documents") as batch_op:
        batch_op.add_column(sa.Column("oggetto", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("richiesta_del", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("provincia", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("comune", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("foglio", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("particella", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("subalterno", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("sezione_urbana", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("tipo_catasto", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("intestati_json", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("dati_immobile_json", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("xml_content", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("visura_subtype", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("situazione_al", sa.String(), nullable=True))

    # visura_properties: restore old columns
    with op.batch_alter_table("visura_properties") as batch_op:
        batch_op.add_column(sa.Column("cadastre_type", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("sheet", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("parcel", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("subunit", sa.String(), nullable=True))

    # visura_requests: restore old columns (now nullable for downgrade safety)
    with op.batch_alter_table("visura_requests") as batch_op:
        batch_op.add_column(sa.Column("tipo_catasto", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("provincia", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("comune", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("foglio", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("particella", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("sezione", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("subalterno", sa.String(), nullable=True))
