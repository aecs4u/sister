"""Flatten response and document payloads into relational child tables.

The legacy JSON/XML columns are intentionally retained as an archive during
this migration.  The new tables are the canonical queryable representation.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260908_flatten_payloads"
down_revision: Union[str, None] = "20260616_rename_birth_place"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def _add_column(table: str, column: sa.Column) -> None:
    if not _has_column(table, column.name):
        op.add_column(table, column)


def _add_foreign_key(source: str, target: str, constraint: str, local: str, remote: str) -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(source) as batch:
            batch.create_foreign_key(constraint, target, [local], [remote])
    else:
        op.create_foreign_key(constraint, source, target, [local], [remote])


def _drop_foreign_key(source: str, constraint: str) -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(source) as batch:
            batch.drop_constraint(constraint, type_="foreignkey")
    else:
        op.drop_constraint(constraint, source, type_="foreignkey")


def upgrade() -> None:
    _add_column("visura_responses", sa.Column("total_results", sa.Integer(), nullable=True))
    _add_column("visura_responses", sa.Column("total_intestati", sa.Integer(), nullable=True))
    _add_column("visura_responses", sa.Column("skipped_soppresso", sa.Integer(), nullable=True))
    _add_column("visura_responses", sa.Column("subject_query", sa.String(), nullable=True))
    _add_column("visura_properties", sa.Column("result_id", sa.Integer(), nullable=True))
    _add_column("visura_owners", sa.Column("result_id", sa.Integer(), nullable=True))
    _add_column("visura_owners", sa.Column("owner_index", sa.Integer(), nullable=True))

    op.create_table(
        "visura_results",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("response_id", sa.String(), sa.ForeignKey("visura_responses.request_id"), nullable=False),
        sa.Column("result_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("visura_present", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_visura_results_response_id", "visura_results", ["response_id"])
    _add_foreign_key("visura_properties", "visura_results", "fk_visura_properties_result_id", "result_id", "id")
    _add_foreign_key("visura_owners", "visura_results", "fk_visura_owners_result_id", "result_id", "id")

    op.create_table(
        "page_visit_form_elements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("page_visit_id", sa.Integer(), sa.ForeignKey("page_visits.id"), nullable=False),
        sa.Column("element_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tag", sa.String(), nullable=False, server_default=""),
        sa.Column("element_type", sa.String(), nullable=False, server_default=""),
        sa.Column("name", sa.String(), nullable=False, server_default=""),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("value", sa.String(), nullable=False, server_default=""),
    )
    op.create_index("ix_page_visit_form_elements_page_visit_id", "page_visit_form_elements", ["page_visit_id"])

    op.create_table(
        "page_visit_errors",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("page_visit_id", sa.Integer(), sa.ForeignKey("page_visits.id"), nullable=False),
        sa.Column("error_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("message", sa.String(), nullable=False, server_default=""),
    )
    op.create_index("ix_page_visit_errors_page_visit_id", "page_visit_errors", ["page_visit_id"])

    op.create_table(
        "document_xml_nodes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("visura_documents.id"), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("document_xml_nodes.id"), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tag", sa.String(), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=True),
    )
    op.create_index("ix_document_xml_nodes_document_id", "document_xml_nodes", ["document_id"])
    op.create_index("ix_document_xml_nodes_parent_id", "document_xml_nodes", ["parent_id"])

    op.create_table(
        "document_xml_attributes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("node_id", sa.Integer(), sa.ForeignKey("document_xml_nodes.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False, server_default=""),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_document_xml_attributes_node_id", "document_xml_attributes", ["node_id"])


def downgrade() -> None:
    op.drop_index("ix_document_xml_attributes_node_id", table_name="document_xml_attributes")
    op.drop_table("document_xml_attributes")
    op.drop_index("ix_document_xml_nodes_parent_id", table_name="document_xml_nodes")
    op.drop_index("ix_document_xml_nodes_document_id", table_name="document_xml_nodes")
    op.drop_table("document_xml_nodes")
    op.drop_index("ix_page_visit_errors_page_visit_id", table_name="page_visit_errors")
    op.drop_table("page_visit_errors")
    op.drop_index("ix_page_visit_form_elements_page_visit_id", table_name="page_visit_form_elements")
    op.drop_table("page_visit_form_elements")
    _drop_foreign_key("visura_owners", "fk_visura_owners_result_id")
    _drop_foreign_key("visura_properties", "fk_visura_properties_result_id")
    op.drop_index("ix_visura_results_response_id", table_name="visura_results")
    op.drop_table("visura_results")
    for table, column in (
        ("visura_owners", "owner_index"),
        ("visura_owners", "result_id"),
        ("visura_properties", "result_id"),
        ("visura_responses", "subject_query"),
        ("visura_responses", "skipped_soppresso"),
        ("visura_responses", "total_intestati"),
        ("visura_responses", "total_results"),
    ):
        if _has_column(table, column):
            op.drop_column(table, column)
