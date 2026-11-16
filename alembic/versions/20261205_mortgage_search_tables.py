"""Create typed mortgage searches and their paid-action records."""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261205_mortgage_search_tables"
down_revision: Union[str, None] = "20261204_billing_transaction_self_reference"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "mortgage_search",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=False),
        sa.Column("inspection_number", sa.String(64), nullable=False),
        sa.Column("inspection_date", sa.Date(), nullable=False),
        sa.Column("office", sa.String(255), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("subject_fiscal_code", sa.String(32), nullable=False),
        sa.Column("requester_fiscal_code", sa.String(32), nullable=True),
        sa.Column("motivation", sa.Text(), nullable=True),
        sa.Column("convention_request_code", sa.String(128), nullable=True),
        sa.Column("formalities_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("annotations_count", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("scope IN ('office', 'national')", name="ck_mortgage_search_scope"),
        sa.ForeignKeyConstraint(["request_id"], [f"{schema}.request.id"], name="fk_mortgage_search_request"),
        sa.PrimaryKeyConstraint("id", name="pk_mortgage_search"),
        sa.UniqueConstraint("request_id", name="uq_mortgage_search_request"),
        sa.UniqueConstraint("inspection_number", "inspection_date", name="uq_mortgage_search_number_date"),
        schema=schema,
    )
    op.create_index(
        "ix_mortgage_search_subject_date", "mortgage_search", ["subject_fiscal_code", "inspection_date"], schema=schema
    )

    op.create_table(
        "mortgage_search_flow",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=False),
        sa.Column("search_id", sa.BigInteger(), nullable=True),
        sa.Column("subject_fiscal_code", sa.String(32), nullable=False),
        sa.Column("office", sa.String(255), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("state", sa.String(24), server_default="preview", nullable=False),
        sa.Column("current_step", sa.String(32), server_default="preview", nullable=False),
        sa.Column(
            "state_payload", sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column("started_by", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope IN ('office', 'national')", name="ck_mortgage_search_flow_scope"),
        sa.CheckConstraint(
            "state IN ('preview', 'accepted', 'running', 'complete', 'uncertain', 'failed', 'cancelled')",
            name="ck_mortgage_search_flow_state",
        ),
        sa.ForeignKeyConstraint(["request_id"], [f"{schema}.request.id"], name="fk_mortgage_search_flow_request"),
        sa.ForeignKeyConstraint(["search_id"], [f"{schema}.mortgage_search.id"], name="fk_mortgage_search_flow_search"),
        sa.PrimaryKeyConstraint("id", name="pk_mortgage_search_flow"),
        sa.UniqueConstraint("request_id", name="uq_mortgage_search_flow_request"),
        schema=schema,
    )
    op.create_index(
        "ix_mortgage_search_flow_subject_state", "mortgage_search_flow", ["subject_fiscal_code", "state"], schema=schema
    )

    op.create_table(
        "mortgage_search_formality",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("search_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("direction", sa.String(24), nullable=True),
        sa.Column("formality_date", sa.Date(), nullable=True),
        sa.Column("registro_particolare", sa.String(64), nullable=True),
        sa.Column("registro_generale", sa.String(64), nullable=True),
        sa.Column("public_official", sa.Text(), nullable=True),
        sa.Column("repertory", sa.String(128), nullable=True),
        sa.Column("repertory_date", sa.Date(), nullable=True),
        sa.Column("act_description", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("electronic_note", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("electronic_title", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("portal_selection_value", sa.Text(), nullable=True),
        sa.Column("note_document_id", sa.BigInteger(), nullable=True),
        sa.Column("title_document_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["search_id"], [f"{schema}.mortgage_search.id"], name="fk_mortgage_search_formality_search"),
        sa.ForeignKeyConstraint(["note_document_id"], [f"{schema}.document.id"], name="fk_mortgage_search_formality_note_document"),
        sa.ForeignKeyConstraint(["title_document_id"], [f"{schema}.document.id"], name="fk_mortgage_search_formality_title_document"),
        sa.PrimaryKeyConstraint("id", name="pk_mortgage_search_formality"),
        sa.UniqueConstraint("search_id", "ordinal", name="uq_mortgage_search_formality_ordinal"),
        schema=schema,
    )
    op.create_index(
        "ix_mortgage_search_formality_register", "mortgage_search_formality",
        ["registro_particolare", "registro_generale"], schema=schema,
    )

    op.create_table(
        "mortgage_search_annotation",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("formality_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("number", sa.String(64), nullable=True),
        sa.Column("annotation_date", sa.Date(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_cancellation", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.ForeignKeyConstraint(
            ["formality_id"], [f"{schema}.mortgage_search_formality.id"], name="fk_mortgage_search_annotation_formality"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mortgage_search_annotation"),
        sa.UniqueConstraint("formality_id", "ordinal", name="uq_mortgage_search_annotation_ordinal"),
        schema=schema,
    )
    op.create_index(
        "ix_mortgage_search_annotation_cancellation", "mortgage_search_annotation", ["is_cancellation"], schema=schema
    )

    op.create_table(
        "mortgage_paid_action",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("flow_id", sa.BigInteger(), nullable=False),
        sa.Column("search_id", sa.BigInteger(), nullable=True),
        sa.Column("step", sa.String(32), nullable=False),
        sa.Column("quote", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confirmed_by", sa.String(255), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(24), server_default="intended", nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clicked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "portal_request_ids", sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"), nullable=False,
        ),
        sa.Column("result", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('intended', 'clicked', 'confirmed', 'uncertain', 'not_executed')",
            name="ck_mortgage_paid_action_state",
        ),
        sa.ForeignKeyConstraint(["flow_id"], [f"{schema}.mortgage_search_flow.id"], name="fk_mortgage_paid_action_flow"),
        sa.ForeignKeyConstraint(["search_id"], [f"{schema}.mortgage_search.id"], name="fk_mortgage_paid_action_search"),
        sa.PrimaryKeyConstraint("id", name="pk_mortgage_paid_action"),
        sa.UniqueConstraint("token_digest", name="uq_mortgage_paid_action_token_digest"),
        schema=schema,
    )
    op.create_index(
        "uq_mortgage_paid_action_open_step", "mortgage_paid_action", ["flow_id", "step"], unique=True,
        postgresql_where=sa.text("state IN ('intended', 'clicked', 'uncertain')"), schema=schema,
    )
    op.create_index(
        "ix_mortgage_paid_action_flow_state", "mortgage_paid_action", ["flow_id", "state"], schema=schema
    )

    op.create_table(
        "mortgage_search_request",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("search_id", sa.BigInteger(), nullable=False),
        sa.Column("formality_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("format", sa.String(16), nullable=True),
        sa.Column("portal_request_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(24), server_default="intended", nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("document_id", sa.BigInteger(), nullable=True),
        sa.Column("paid_action_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("kind IN ('note', 'title')", name="ck_mortgage_search_request_kind"),
        sa.CheckConstraint(
            "status IN ('intended', 'requested', 'available', 'downloaded', 'failed', 'uncertain')",
            name="ck_mortgage_search_request_status",
        ),
        sa.ForeignKeyConstraint(["search_id"], [f"{schema}.mortgage_search.id"], name="fk_mortgage_search_request_search"),
        sa.ForeignKeyConstraint(
            ["formality_id"], [f"{schema}.mortgage_search_formality.id"], name="fk_mortgage_search_request_formality"
        ),
        sa.ForeignKeyConstraint(["document_id"], [f"{schema}.document.id"], name="fk_mortgage_search_request_document"),
        sa.ForeignKeyConstraint(
            ["paid_action_id"], [f"{schema}.mortgage_paid_action.id"], name="fk_mortgage_search_request_paid_action"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mortgage_search_request"),
        sa.UniqueConstraint("portal_request_id", name="uq_mortgage_search_request_portal_id"),
        schema=schema,
    )
    op.create_index(
        "ix_mortgage_search_request_flow", "mortgage_search_request", ["search_id", "kind", "status"], schema=schema
    )
    op.create_index(
        "ix_mortgage_search_request_formality", "mortgage_search_request", ["formality_id", "kind", "status"], schema=schema
    )

    op.create_table(
        "mortgage_search_charge",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("search_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), server_default="EUR", nullable=False),
        sa.Column("charged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("portal_request_id", sa.String(64), nullable=True),
        sa.Column("basis", sa.String(24), nullable=False),
        sa.Column("receipt_id", sa.BigInteger(), nullable=True),
        sa.Column("wallet_tx_id", sa.String(128), nullable=True),
        sa.CheckConstraint("kind IN ('inspection', 'note', 'title')", name="ck_mortgage_search_charge_kind"),
        sa.CheckConstraint("basis IN ('cost_page', 'receipt', 'statement_detail', 'tariff')", name="ck_mortgage_search_charge_basis"),
        sa.CheckConstraint("quantity > 0 AND unit_amount >= 0 AND amount >= 0", name="ck_mortgage_search_charge_amounts"),
        sa.ForeignKeyConstraint(["search_id"], [f"{schema}.mortgage_search.id"], name="fk_mortgage_search_charge_search"),
        sa.PrimaryKeyConstraint("id", name="pk_mortgage_search_charge"),
        schema=schema,
    )
    op.create_index(
        "ix_mortgage_search_charge_search_kind", "mortgage_search_charge", ["search_id", "kind"], schema=schema
    )
    op.create_index(
        "ix_mortgage_search_charge_portal_request", "mortgage_search_charge", ["portal_request_id"], schema=schema
    )


def downgrade() -> None:
    schema = _schema()
    for table in (
        "mortgage_search_charge",
        "mortgage_search_request",
        "mortgage_paid_action",
        "mortgage_search_annotation",
        "mortgage_search_formality",
        "mortgage_search_flow",
        "mortgage_search",
    ):
        op.drop_table(table, schema=schema)
