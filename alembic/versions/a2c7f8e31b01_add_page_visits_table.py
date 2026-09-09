"""add page_visits table

Revision ID: a2c7f8e31b01
Revises: 1305de055bf4
Create Date: 2026-04-12 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2c7f8e31b01"
down_revision: Union[str, None] = "1305de055bf4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "page_visits",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("response_id", sa.String(), nullable=False),
        sa.Column("step", sa.String(), nullable=False, server_default=""),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("screenshot_url", sa.String(), nullable=True),
        sa.Column("form_elements_json", sa.Text(), nullable=True),
        sa.Column("errors_json", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["response_id"], ["visura_responses.request_id"]),
    )
    op.create_index("ix_page_visits_response_id", "page_visits", ["response_id"])


def downgrade() -> None:
    op.drop_index("ix_page_visits_response_id", table_name="page_visits")
    op.drop_table("page_visits")
