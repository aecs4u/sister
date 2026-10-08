"""Store Ocular structured document payloads and normalized inspection rows."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261009_ocular_structured"
down_revision: Union[str, None] = "20261006_pk_defaults"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_structured_extractions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("schema_name", sa.String(), nullable=False),
        sa.Column("schema_version", sa.String(), nullable=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=True),
        sa.Column("output_path", sa.String(), nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("structured_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["visura_documents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "schema_name", "run_id", name="uq_structured_extraction_run"),
    )
    op.create_index("ix_document_structured_extractions_document_id", "document_structured_extractions", ["document_id"])
    op.create_index("idx_structured_extraction_latest", "document_structured_extractions", ["document_id", "created_at"])

    op.create_table(
        "mortgage_inspections",
        sa.Column("extraction_id", sa.Integer(), nullable=False),
        sa.Column("inspection_date", sa.String(), nullable=True),
        sa.Column("inspection_time", sa.String(), nullable=True),
        sa.Column("inspection_number", sa.String(), nullable=True),
        sa.Column("inspection_date_number", sa.String(), nullable=True),
        sa.Column("inspection_start", sa.String(), nullable=True),
        sa.Column("requester", sa.String(), nullable=True),
        sa.Column("tax_paid_euro", sa.Integer(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("office", sa.String(), nullable=True),
        sa.Column("service", sa.String(), nullable=True),
        sa.Column("note_type", sa.String(), nullable=True),
        sa.Column("note_timestamp", sa.String(), nullable=True),
        sa.Column("registro_generale", sa.Integer(), nullable=True),
        sa.Column("registro_particolare", sa.Integer(), nullable=True),
        sa.Column("presentazione_numero", sa.Integer(), nullable=True),
        sa.Column("presentazione_data", sa.String(), nullable=True),
        sa.Column("section_a_other_data", sa.Text(), nullable=True),
        sa.Column("section_d_text", sa.Text(), nullable=True),
        sa.Column("unit_count", sa.Integer(), nullable=True),
        sa.Column("party_favore_count", sa.Integer(), nullable=True),
        sa.Column("party_contro_count", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["extraction_id"], ["document_structured_extractions.id"]),
        sa.PrimaryKeyConstraint("extraction_id"),
    )

    op.create_table(
        "mortgage_inspection_titles",
        sa.Column("extraction_id", sa.Integer(), nullable=False),
        sa.Column("title_type", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("title_date", sa.String(), nullable=True),
        sa.Column("repertory_number", sa.String(), nullable=True),
        sa.Column("notary_name", sa.String(), nullable=True),
        sa.Column("notary_fiscal_code", sa.String(), nullable=True),
        sa.Column("notary_location", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["extraction_id"], ["document_structured_extractions.id"]),
        sa.PrimaryKeyConstraint("extraction_id"),
    )

    op.create_table(
        "mortgage_inspection_liens",
        sa.Column("extraction_id", sa.Integer(), nullable=False),
        sa.Column("lien_type", sa.String(), nullable=True),
        sa.Column("lien_type_original", sa.String(), nullable=True),
        sa.Column("derived_from", sa.Text(), nullable=True),
        sa.Column("derived_from_code", sa.String(), nullable=True),
        sa.Column("derived_from_description", sa.Text(), nullable=True),
        sa.Column("principal_euro", sa.Integer(), nullable=True),
        sa.Column("annual_interest_rate", sa.String(), nullable=True),
        sa.Column("annual_interest_rate_pct", sa.Float(), nullable=True),
        sa.Column("semiannual_interest_rate", sa.String(), nullable=True),
        sa.Column("interest_euro", sa.Integer(), nullable=True),
        sa.Column("expenses_euro", sa.Integer(), nullable=True),
        sa.Column("total_euro", sa.Integer(), nullable=True),
        sa.Column("variable_amounts", sa.Boolean(), nullable=True),
        sa.Column("foreign_currency", sa.String(), nullable=True),
        sa.Column("automatic_increase", sa.Boolean(), nullable=True),
        sa.Column("resolutive_condition", sa.Boolean(), nullable=True),
        sa.Column("duration_years", sa.Integer(), nullable=True),
        sa.Column("duration_description", sa.String(), nullable=True),
        sa.Column("mortgage_rank", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["extraction_id"], ["document_structured_extractions.id"]),
        sa.PrimaryKeyConstraint("extraction_id"),
    )

    op.create_table(
        "mortgage_inspection_units",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("extraction_id", sa.Integer(), nullable=False),
        sa.Column("unit_number", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["extraction_id"], ["document_structured_extractions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("extraction_id", "unit_number", name="uq_mortgage_inspection_unit"),
    )
    op.create_index("ix_mortgage_inspection_units_extraction_id", "mortgage_inspection_units", ["extraction_id"])

    op.create_table(
        "mortgage_inspection_properties",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("unit_id", sa.Integer(), nullable=False),
        sa.Column("property_number", sa.Integer(), nullable=True),
        sa.Column("municipality_code", sa.String(), nullable=True),
        sa.Column("municipality", sa.String(), nullable=True),
        sa.Column("cadastre_type", sa.String(), nullable=True),
        sa.Column("urban_section", sa.String(), nullable=True),
        sa.Column("sheet", sa.Integer(), nullable=True),
        sa.Column("parcel", sa.Integer(), nullable=True),
        sa.Column("subunit", sa.Integer(), nullable=True),
        sa.Column("nature", sa.String(), nullable=True),
        sa.Column("nature_description", sa.String(), nullable=True),
        sa.Column("room_count", sa.Integer(), nullable=True),
        sa.Column("area_sqm", sa.Float(), nullable=True),
        sa.Column("floor", sa.String(), nullable=True),
        sa.Column("address", sa.String(), nullable=True),
        sa.Column("civic_number", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["unit_id"], ["mortgage_inspection_units.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mortgage_inspection_properties_unit_id", "mortgage_inspection_properties", ["unit_id"])

    op.create_table(
        "mortgage_inspection_parties",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("extraction_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("subject_number", sa.Integer(), nullable=True),
        sa.Column("quality", sa.String(), nullable=True),
        sa.Column("surname", sa.String(), nullable=True),
        sa.Column("given_name", sa.String(), nullable=True),
        sa.Column("birth_date", sa.String(), nullable=True),
        sa.Column("birth_place", sa.String(), nullable=True),
        sa.Column("gender", sa.String(), nullable=True),
        sa.Column("fiscal_code", sa.String(), nullable=True),
        sa.Column("entity_name", sa.String(), nullable=True),
        sa.Column("registered_office", sa.String(), nullable=True),
        sa.Column("mortgage_domicile", sa.String(), nullable=True),
        sa.Column("negotiation_unit_reference", sa.Integer(), nullable=True),
        sa.Column("right_type", sa.String(), nullable=True),
        sa.Column("share_numerator", sa.Integer(), nullable=True),
        sa.Column("share_denominator", sa.Integer(), nullable=True),
        sa.Column("raw_quality", sa.String(), nullable=True),
        sa.Column("raw_share", sa.String(), nullable=True),
        sa.Column("raw_right_type", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["extraction_id"], ["document_structured_extractions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("extraction_id", "role", "ordinal", name="uq_mortgage_inspection_party"),
    )
    op.create_index("ix_mortgage_inspection_parties_extraction_id", "mortgage_inspection_parties", ["extraction_id"])


def downgrade() -> None:
    op.drop_index("ix_mortgage_inspection_parties_extraction_id", table_name="mortgage_inspection_parties")
    op.drop_table("mortgage_inspection_parties")
    op.drop_index("ix_mortgage_inspection_properties_unit_id", table_name="mortgage_inspection_properties")
    op.drop_table("mortgage_inspection_properties")
    op.drop_index("ix_mortgage_inspection_units_extraction_id", table_name="mortgage_inspection_units")
    op.drop_table("mortgage_inspection_units")
    op.drop_table("mortgage_inspection_liens")
    op.drop_table("mortgage_inspection_titles")
    op.drop_table("mortgage_inspections")
    op.drop_index("idx_structured_extraction_latest", table_name="document_structured_extractions")
    op.drop_index("ix_document_structured_extractions_document_id", table_name="document_structured_extractions")
    op.drop_table("document_structured_extractions")
