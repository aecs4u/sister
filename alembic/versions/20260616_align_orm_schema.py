"""align DB schema with current ORM models

- visura_responses: rename tipo_catasto → cadastre_type
- visura_properties: add subject_id FK, drop company_name / fiscal_code
- visura_owners: add subject_id + right_id FKs, drop denormalised columns

Revision ID: 20260616_align_orm_schema
Revises: 20260616_drop_legacy
Create Date: 2026-06-16
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260616_align_orm_schema"
down_revision: Union[str, None] = "20260616_drop_legacy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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
    # ── 1. visura_responses: rename tipo_catasto → cadastre_type ─────────────
    if _column_exists("visura_responses", "tipo_catasto"):
        with op.batch_alter_table("visura_responses", recreate="always") as batch_op:
            batch_op.alter_column("tipo_catasto", new_column_name="cadastre_type")

    # ── 2. visura_properties: add subject_id; drop denormalised entity fields ─
    with op.batch_alter_table("visura_properties", recreate="always") as batch_op:
        if not _column_exists("visura_properties", "subject_id"):
            batch_op.add_column(sa.Column("subject_id", sa.Integer(), nullable=True))
            batch_op.create_index("ix_visura_properties_subject_id", ["subject_id"])
        if _column_exists("visura_properties", "company_name"):
            batch_op.drop_column("company_name")
        if _column_exists("visura_properties", "fiscal_code"):
            if _index_exists("ix_visura_properties_fiscal_code"):
                batch_op.drop_index("ix_visura_properties_fiscal_code")
            batch_op.drop_column("fiscal_code")

    # ── 3. visura_owners: add subject_id + right_id; drop old flat columns ────
    with op.batch_alter_table("visura_owners", recreate="always") as batch_op:
        if not _column_exists("visura_owners", "subject_id"):
            batch_op.add_column(sa.Column("subject_id", sa.Integer(), nullable=True))
            batch_op.create_index("ix_visura_owners_subject_id", ["subject_id"])
        if not _column_exists("visura_owners", "right_id"):
            batch_op.add_column(sa.Column("right_id", sa.Integer(), nullable=True))
            batch_op.create_index("ix_visura_owners_right_id", ["right_id"])
        if _column_exists("visura_owners", "fiscal_code") and _index_exists("ix_visura_owners_fiscal_code"):
            batch_op.drop_index("ix_visura_owners_fiscal_code")
        for col in ("full_name", "fiscal_code", "right_type", "ownership_share"):
            if _column_exists("visura_owners", col):
                batch_op.drop_column(col)


def downgrade() -> None:
    # visura_owners: restore denormalised columns
    with op.batch_alter_table("visura_owners") as batch_op:
        batch_op.add_column(sa.Column("full_name", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("fiscal_code", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("right_type", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("ownership_share", sa.String(), nullable=True))
        if _column_exists("visura_owners", "subject_id"):
            batch_op.drop_index("ix_visura_owners_subject_id")
            batch_op.drop_column("subject_id")
        if _column_exists("visura_owners", "right_id"):
            batch_op.drop_index("ix_visura_owners_right_id")
            batch_op.drop_column("right_id")

    # visura_properties: restore denormalised columns
    with op.batch_alter_table("visura_properties") as batch_op:
        batch_op.add_column(sa.Column("company_name", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("fiscal_code", sa.String(), nullable=True))
        if _column_exists("visura_properties", "subject_id"):
            batch_op.drop_index("ix_visura_properties_subject_id")
            batch_op.drop_column("subject_id")

    # visura_responses: rename back
    if _column_exists("visura_responses", "cadastre_type"):
        with op.batch_alter_table("visura_responses") as batch_op:
            batch_op.alter_column("cadastre_type", new_column_name="tipo_catasto")
