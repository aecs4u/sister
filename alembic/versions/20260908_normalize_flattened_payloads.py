"""Add normalization constraints to flattened response/document tables."""

from typing import Sequence, Union

from alembic import op

revision: str = "20260908_normalize_payloads"
down_revision: Union[str, None] = "20260908_flatten_payloads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.create_index("uq_visura_results_response_index", "visura_results", ["response_id", "result_index"], unique=True)
        op.create_index(
            "uq_page_visit_form_elements_visit_index", "page_visit_form_elements", ["page_visit_id", "element_index"], unique=True
        )
        op.create_index("uq_page_visit_errors_visit_index", "page_visit_errors", ["page_visit_id", "error_index"], unique=True)
        op.create_index(
            "uq_document_xml_attributes_node_name", "document_xml_attributes", ["node_id", "name"], unique=True
        )
    else:
        op.create_unique_constraint(
            "uq_visura_results_response_index", "visura_results", ["response_id", "result_index"]
        )
        op.create_unique_constraint(
            "uq_page_visit_form_elements_visit_index", "page_visit_form_elements", ["page_visit_id", "element_index"]
        )
        op.create_unique_constraint(
            "uq_page_visit_errors_visit_index", "page_visit_errors", ["page_visit_id", "error_index"]
        )
        op.create_unique_constraint(
            "uq_document_xml_attributes_node_name", "document_xml_attributes", ["node_id", "name"]
        )
    op.create_index(
        "ix_visura_properties_result_id", "visura_properties", ["result_id"], unique=False
    )
    op.create_index("ix_visura_owners_result_id", "visura_owners", ["result_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_visura_owners_result_id", table_name="visura_owners")
    op.drop_index("ix_visura_properties_result_id", table_name="visura_properties")
    if op.get_bind().dialect.name == "sqlite":
        op.drop_index("uq_document_xml_attributes_node_name", table_name="document_xml_attributes")
        op.drop_index("uq_page_visit_errors_visit_index", table_name="page_visit_errors")
        op.drop_index("uq_page_visit_form_elements_visit_index", table_name="page_visit_form_elements")
        op.drop_index("uq_visura_results_response_index", table_name="visura_results")
    else:
        op.drop_constraint("uq_document_xml_attributes_node_name", "document_xml_attributes", type_="unique")
        op.drop_constraint("uq_page_visit_errors_visit_index", "page_visit_errors", type_="unique")
        op.drop_constraint("uq_page_visit_form_elements_visit_index", "page_visit_form_elements", type_="unique")
        op.drop_constraint("uq_visura_results_response_index", "visura_results", type_="unique")
