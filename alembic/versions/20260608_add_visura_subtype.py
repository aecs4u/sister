"""add visura_subtype to visura_documents

Revision ID: 20260608_add_visura_subtype
Revises: b3d8f2a41c01
Create Date: 2026-06-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260608_add_visura_subtype"
down_revision: Union[str, None] = ("b3d8f2a41c01", "20260530_drop_workflow")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("visura_documents", sa.Column("visura_subtype", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("visura_documents", "visura_subtype")
