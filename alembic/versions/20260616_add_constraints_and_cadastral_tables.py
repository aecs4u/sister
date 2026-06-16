"""add constraints and new cadastral tables

- cadastral_subjects: replace non-unique fiscal_code index with partial unique index
- polymorphic tables: add CHECK constraints enforcing exactly-one-parent FK
- create new cadastral.py tables (cadastral_queries and related)

Revision ID: 20260616_add_constraints
Revises: 20260616_align_orm_schema
Create Date: 2026-06-16
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260616_add_constraints"
down_revision: Union[str, None] = "20260616_align_orm_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    ).fetchone()
    return bool(row and row[0])


def _index_exists(index_name: str) -> bool:
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT count(*) FROM sqlite_master WHERE type='index' AND name=:n"),
        {"n": index_name},
    ).fetchone()
    return bool(row and row[0])


def _constraint_exists(table: str, constraint_name: str) -> bool:
    """Check if a named CHECK constraint exists in the CREATE TABLE SQL."""
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    ).fetchone()
    return bool(row and row[0] and constraint_name in row[0])


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. cadastral_subjects: swap non-unique index for partial unique index ─
    if _index_exists("ix_cadastral_subjects_fiscal_code"):
        conn.execute(sa.text("DROP INDEX ix_cadastral_subjects_fiscal_code"))
    if not _index_exists("uq_subject_fiscal_code"):
        conn.execute(sa.text(
            "CREATE UNIQUE INDEX uq_subject_fiscal_code "
            "ON cadastral_subjects (fiscal_code) WHERE fiscal_code IS NOT NULL"
        ))

    # ── 2. Polymorphic tables: add CHECK constraints via batch alter ───────────
    #
    # SQLite does not support ADD CONSTRAINT after table creation; batch_alter_table
    # with recreate="always" rebuilds the table with the new constraint in place.
    # Existing rows are preserved; rows that violate exactly-one-parent would cause
    # an error here, but no such rows should exist if the XML loader is correct.

    if not _constraint_exists("building_identifiers", "ck_building_identifier_one_parent"):
        with op.batch_alter_table("building_identifiers", recreate="always") as batch_op:
            batch_op.create_check_constraint(
                "ck_building_identifier_one_parent",
                "(building_unit_id IS NOT NULL) + (current_state_id IS NOT NULL) + (history_document_id IS NOT NULL) = 1",
            )

    if not _constraint_exists("building_classifications", "ck_building_classification_one_parent"):
        with op.batch_alter_table("building_classifications", recreate="always") as batch_op:
            batch_op.create_check_constraint(
                "ck_building_classification_one_parent",
                "(building_unit_id IS NOT NULL) + (current_state_id IS NOT NULL) + (history_document_id IS NOT NULL) = 1",
            )

    if not _constraint_exists("building_surfaces", "ck_building_surface_one_parent"):
        with op.batch_alter_table("building_surfaces", recreate="always") as batch_op:
            batch_op.create_check_constraint(
                "ck_building_surface_one_parent",
                "(building_unit_id IS NOT NULL) + (current_state_id IS NOT NULL) = 1",
            )

    if not _constraint_exists("related_parcels", "ck_related_parcel_one_parent"):
        with op.batch_alter_table("related_parcels", recreate="always") as batch_op:
            batch_op.create_check_constraint(
                "ck_related_parcel_one_parent",
                "(building_unit_id IS NOT NULL) + (current_state_id IS NOT NULL) = 1",
            )

    if not _constraint_exists("ownership_mutations", "ck_ownership_mutation_one_parent"):
        with op.batch_alter_table("ownership_mutations", recreate="always") as batch_op:
            batch_op.create_check_constraint(
                "ck_ownership_mutation_one_parent",
                "(document_id IS NOT NULL) + (property_group_id IS NOT NULL) + (land_parcel_id IS NOT NULL) = 1",
            )

    # ── 3. Create new cadastral.py tables ────────────────────────────────────
    #
    # These tables did not exist before cadastral.py was added.
    # init_db() also creates them via _SISTER_TABLES; this migration covers the
    # alembic upgrade path for existing deployments.

    if not _table_exists("cadastral_queries"):
        op.create_table(
            "cadastral_queries",
            sa.Column("query_type", sa.String(), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("endpoint", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("timestamp", sa.DateTime(), nullable=False),
            sa.Column("owner", sa.String(), nullable=False),
            sa.Column("query_datetime", sa.DateTime(), nullable=True),
            sa.Column("entity_type", sa.String(), nullable=True),
            sa.Column("result", sa.JSON(), nullable=True),
            sa.Column("source_timestamp", sa.Integer(), nullable=True),
            sa.Column("scope", sa.String(), nullable=True),
            sa.Column("callback", sa.Boolean(), nullable=True),
            sa.Column("outcome", sa.String(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _table_exists("cadastral_inspections"):
        op.create_table(
            "cadastral_inspections",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("entity", sa.String(), nullable=False),
            sa.Column("callback", sa.Boolean(), nullable=False),
            sa.Column("inspection_type", sa.String(), nullable=False),
            sa.Column("requester", sa.String(), nullable=False),
            sa.Column("document", sa.String(), nullable=False),
            sa.Column("outcome", sa.String(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _table_exists("cadastral_location_parameters"):
        op.create_table(
            "cadastral_location_parameters",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("query_id", sa.String(), sa.ForeignKey("cadastral_queries.id"), nullable=True),
            sa.Column("inspection_id", sa.String(), sa.ForeignKey("cadastral_inspections.id"), nullable=True),
            sa.Column("location_id", sa.Integer(), sa.ForeignKey("cadastral_locations.id"), nullable=True),
            sa.Column("property_id", sa.String(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_cadastral_location_parameters_query_id", "cadastral_location_parameters", ["query_id"])
        op.create_index("ix_cadastral_location_parameters_inspection_id", "cadastral_location_parameters", ["inspection_id"])
        op.create_index("ix_cadastral_location_parameters_location_id", "cadastral_location_parameters", ["location_id"])

    if not _table_exists("cadastral_property_properties"):
        op.create_table(
            "cadastral_property_properties",
            sa.Column("id", sa.String(), sa.ForeignKey("cadastral_queries.id"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _table_exists("cadastral_prospect_properties"):
        op.create_table(
            "cadastral_prospect_properties",
            sa.Column("id", sa.String(), sa.ForeignKey("cadastral_queries.id"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _table_exists("cadastral_prospect_owners"):
        op.create_table(
            "cadastral_prospect_owners",
            sa.Column("id", sa.String(), sa.ForeignKey("cadastral_queries.id"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _table_exists("legal_entity_search_parameters"):
        op.create_table(
            "legal_entity_search_parameters",
            sa.Column("id", sa.String(), sa.ForeignKey("cadastral_queries.id"), nullable=False),
            sa.Column("tax_code", sa.String(), nullable=False),
            sa.Column("cadastre_type", sa.String(), nullable=False),
            sa.Column("province", sa.String(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _table_exists("legal_entity_search_entities"):
        op.create_table(
            "legal_entity_search_entities",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("query_id", sa.String(), sa.ForeignKey("cadastral_queries.id"), nullable=False),
            sa.Column("subject_id", sa.Integer(), sa.ForeignKey("cadastral_subjects.id"), nullable=True),
            sa.Column("birth_place_text", sa.String(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_legal_entity_search_entities_subject_id", "legal_entity_search_entities", ["subject_id"])

    if not _table_exists("legal_entity_search_geo_summaries"):
        op.create_table(
            "legal_entity_search_geo_summaries",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("entity_id", sa.String(), sa.ForeignKey("legal_entity_search_entities.id"), nullable=False),
            sa.Column("geo_type", sa.String(), nullable=False),
            sa.Column("town", sa.String(), nullable=True),
            sa.Column("province", sa.String(), nullable=True),
            sa.Column("municipality", sa.String(), nullable=True),
            sa.Column("buildings", sa.Integer(), nullable=False),
            sa.Column("lands", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_legal_entity_search_geo_summaries_entity_id", "legal_entity_search_geo_summaries", ["entity_id"])

    if not _table_exists("legal_entity_search_properties"):
        op.create_table(
            "legal_entity_search_properties",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("land_registry", sa.String(), nullable=False),
            sa.Column("ownership", sa.String(), nullable=False),
            sa.Column("location", sa.String(), nullable=False),
            sa.Column("location_id", sa.Integer(), sa.ForeignKey("cadastral_locations.id"), nullable=True),
            sa.Column("cadastral_code", sa.String(), nullable=False),
            sa.Column("classification", sa.String(), nullable=False),
            sa.Column("cadastral_class", sa.String(), nullable=False),
            sa.Column("consistency", sa.String(), nullable=False),
            sa.Column("income", sa.String(), nullable=False),
            sa.Column("property_id", sa.String(), nullable=False),
            sa.Column("entity_id", sa.String(), sa.ForeignKey("legal_entity_search_entities.id"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_legal_entity_search_properties_location_id", "legal_entity_search_properties", ["location_id"])


def downgrade() -> None:
    conn = op.get_bind()

    # Drop new tables in reverse dependency order
    for table in [
        "legal_entity_search_properties",
        "legal_entity_search_geo_summaries",
        "legal_entity_search_entities",
        "legal_entity_search_parameters",
        "cadastral_prospect_owners",
        "cadastral_prospect_properties",
        "cadastral_property_properties",
        "cadastral_location_parameters",
        "cadastral_inspections",
        "cadastral_queries",
    ]:
        if _table_exists(table):
            conn.execute(sa.text(f"DROP TABLE {table}"))

    # Restore non-unique fiscal_code index
    if _index_exists("uq_subject_fiscal_code"):
        conn.execute(sa.text("DROP INDEX uq_subject_fiscal_code"))
    if not _index_exists("ix_cadastral_subjects_fiscal_code"):
        conn.execute(sa.text("CREATE INDEX ix_cadastral_subjects_fiscal_code ON cadastral_subjects (fiscal_code)"))

    # CHECK constraints: removing them requires table recreation; omitted from downgrade
    # as reverting this migration is unlikely in production.
