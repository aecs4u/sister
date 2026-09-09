"""add visura_documents table

Revision ID: b3d8f2a41c01
Revises: a2c7f8e31b01
Create Date: 2026-04-12 14:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "b3d8f2a41c01"
down_revision: Union[str, None] = "a2c7f8e31b01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "visura_documents",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("response_id", sa.String(), nullable=True),
        sa.Column("document_type", sa.String(), nullable=False, server_default=""),
        sa.Column("file_format", sa.String(), nullable=False, server_default=""),
        sa.Column("filename", sa.String(), nullable=False, server_default=""),
        sa.Column("file_path", sa.String(), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("oggetto", sa.String(), nullable=True),
        sa.Column("richiesta_del", sa.String(), nullable=True),
        sa.Column("provincia", sa.String(), nullable=True),
        sa.Column("comune", sa.String(), nullable=True),
        sa.Column("foglio", sa.String(), nullable=True),
        sa.Column("particella", sa.String(), nullable=True),
        sa.Column("subalterno", sa.String(), nullable=True),
        sa.Column("sezione_urbana", sa.String(), nullable=True),
        sa.Column("tipo_catasto", sa.String(), nullable=True),
        sa.Column("intestati_json", sa.Text(), nullable=True),
        sa.Column("dati_immobile_json", sa.Text(), nullable=True),
        sa.Column("xml_content", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["response_id"], ["visura_responses.request_id"]),
    )
    op.create_index("ix_visura_documents_response_id", "visura_documents", ["response_id"])
    op.create_index("idx_documents_lookup", "visura_documents", ["provincia", "comune", "foglio", "particella"])


def downgrade() -> None:
    op.drop_index("idx_documents_lookup", table_name="visura_documents")
    op.drop_index("ix_visura_documents_response_id", table_name="visura_documents")
    op.drop_table("visura_documents")
