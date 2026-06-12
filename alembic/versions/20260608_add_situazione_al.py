"""add situazione_al to visura_documents

Revision ID: 20260608_add_situazione_al
Revises: 20260608_add_visura_subtype
Create Date: 2026-06-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260608_add_situazione_al"
down_revision: Union[str, None] = "20260608_add_visura_subtype"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("visura_documents", sa.Column("situazione_al", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("visura_documents", "situazione_al")
