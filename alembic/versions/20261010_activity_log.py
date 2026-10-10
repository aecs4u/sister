"""Add ``activity_log``: a database record of the operational actions (imports, rescans, browser control, backfills).

One row per action with who ran it, from where, the parameters, the outcome and the time it took, so an import or a
re-import of documents can be traced afterwards. Idempotent: an existing table is left alone.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261010_activity_log"
down_revision: Union[str, None] = "20261010_xml_typed_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None

TABLE = "activity_log"


def upgrade() -> None:
    if TABLE in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("actor", sa.String(), nullable=False, server_default="system"),
        sa.Column("source", sa.String(), nullable=False, server_default="web"),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="success"),
        sa.Column("target", sa.String(), nullable=True),
        sa.Column("params", postgresql.JSONB(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("client_ip", sa.String(), nullable=True),
    )
    op.create_index(f"ix_{TABLE}_started_at", TABLE, ["started_at"])
    op.create_index(f"ix_{TABLE}_action", TABLE, ["action"])
    op.create_index(f"ix_{TABLE}_actor", TABLE, ["actor"])
    op.create_index(f"ix_{TABLE}_status", TABLE, ["status"])


def downgrade() -> None:
    if TABLE in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table(TABLE)
