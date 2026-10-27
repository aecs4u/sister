"""Track whether an OMI selection is an automatic estimate or a user choice."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261112_omi_selection_origin"
down_revision: Union[str, None] = "20261112_property_actual_size"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    import os

    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    op.add_column(
        "property_omi_selection",
        sa.Column("is_manual", sa.Boolean(), nullable=True),
        schema=_schema(),
    )


def downgrade() -> None:
    op.drop_column("property_omi_selection", "is_manual", schema=_schema())
