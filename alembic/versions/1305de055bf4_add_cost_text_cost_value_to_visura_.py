"""add cost_text cost_value to visura_requests

Revision ID: 1305de055bf4
Revises: 31f03df3db46
Create Date: 2026-04-12 07:23:01.066773
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "1305de055bf4"
down_revision: Union[str, None] = "31f03df3db46"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("visura_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cost_text", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("cost_value", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("visura_requests", schema=None) as batch_op:
        batch_op.drop_column("cost_value")
        batch_op.drop_column("cost_text")
