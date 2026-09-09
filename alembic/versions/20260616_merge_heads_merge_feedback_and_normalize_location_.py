"""merge feedback and normalize-location branches

Revision ID: 20260616_merge_heads
Revises: 20260611_add_feedback_config, 20260616_normalize_location
Create Date: 2026-06-16 09:37:15.136048
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '20260616_merge_heads'
down_revision: Union[str, None] = ('20260611_add_feedback_config', '20260616_normalize_location')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
