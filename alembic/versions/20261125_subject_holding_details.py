"""``subject_holding``: how many properties were valued with an assumed 1/1 share, and the date of the search.

``unknown_share`` counts the properties whose owner row has no quota (the pro quota value counts them in full);
``searched_at`` is when the subject search the figures come from was answered. Refill with
``sister db backfill holdings`` after upgrading.
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261125_holding_details"
down_revision: Union[str, None] = "20261124_subject_holding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    inspector = sa.inspect(op.get_bind())
    existing = {c["name"] for c in inspector.get_columns("subject_holding", schema=schema)}
    if "unknown_share" not in existing:
        op.add_column("subject_holding", sa.Column("unknown_share", sa.Integer(), nullable=True), schema=schema)
    if "searched_at" not in existing:
        op.add_column(
            "subject_holding", sa.Column("searched_at", sa.DateTime(timezone=True), nullable=True), schema=schema
        )


def downgrade() -> None:
    schema = _schema()
    op.drop_column("subject_holding", "searched_at", schema=schema)
    op.drop_column("subject_holding", "unknown_share", schema=schema)
