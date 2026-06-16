"""Fix prospect tables cardinality and location_parameters uniqueness

- Recreate cadastral_prospect_properties / cadastral_prospect_owners with
  surrogate int PK + query_id FK column (previously PK=FK, allowing only one
  row per query while the relationship is list[...]).
- Add partial unique indexes on cadastral_location_parameters.query_id and
  .inspection_id to enforce the singular Optional[...] relationship contract.

Revision ID: 20260616_fix_prospect_cardinality
Revises: 20260616_add_constraints
Create Date: 2026-06-16
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260616_fix_prospect_cardinality"
down_revision: Union[str, None] = "20260616_add_constraints"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"), {"n": name})
    return result.fetchone() is not None


def _index_exists(name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='index' AND name=:n"), {"n": name})
    return result.fetchone() is not None


def upgrade() -> None:
    # ── Recreate prospect tables with surrogate PK ────────────────────────────
    for table in ("cadastral_prospect_properties", "cadastral_prospect_owners"):
        if _table_exists(table):
            op.drop_table(table)

    op.create_table(
        "cadastral_prospect_properties",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("query_id", sa.String(), sa.ForeignKey("cadastral_queries.id"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cadastral_prospect_properties_query_id", "cadastral_prospect_properties", ["query_id"])

    op.create_table(
        "cadastral_prospect_owners",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("query_id", sa.String(), sa.ForeignKey("cadastral_queries.id"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cadastral_prospect_owners_query_id", "cadastral_prospect_owners", ["query_id"])

    # ── Partial unique indexes on location_parameters ─────────────────────────
    if not _index_exists("uq_location_params_query_id"):
        op.get_bind().execute(
            sa.text(
                "CREATE UNIQUE INDEX uq_location_params_query_id "
                "ON cadastral_location_parameters (query_id) "
                "WHERE query_id IS NOT NULL"
            )
        )

    if not _index_exists("uq_location_params_inspection_id"):
        op.get_bind().execute(
            sa.text(
                "CREATE UNIQUE INDEX uq_location_params_inspection_id "
                "ON cadastral_location_parameters (inspection_id) "
                "WHERE inspection_id IS NOT NULL"
            )
        )


def downgrade() -> None:
    # Remove partial unique indexes
    for idx in ("uq_location_params_query_id", "uq_location_params_inspection_id"):
        if _index_exists(idx):
            op.get_bind().execute(sa.text(f"DROP INDEX {idx}"))

    # Revert prospect tables to PK=FK schema
    for table in ("cadastral_prospect_properties", "cadastral_prospect_owners"):
        if _table_exists(table):
            op.drop_table(table)

    op.create_table(
        "cadastral_prospect_properties",
        sa.Column("id", sa.String(), sa.ForeignKey("cadastral_queries.id"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "cadastral_prospect_owners",
        sa.Column("id", sa.String(), sa.ForeignKey("cadastral_queries.id"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
