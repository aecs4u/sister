"""drop workflow_runs and workflow_steps tables

The workflow engine has been migrated to opendata. Sister retains only the
atomic cadastral scraping primitives; workflow orchestration and persistence
now live in opendata/opendata/workflows/.

Revision ID: 20260530_drop_workflow
Revises: 1305de055bf4
Create Date: 2026-05-30

"""

import sqlalchemy as sa
from alembic import op

revision = "20260530_drop_workflow"
down_revision = "1305de055bf4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("workflow_steps")
    op.drop_table("workflow_runs")


def downgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("preset", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=True),
        sa.Column("output_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("workflow_id"),
    )
    op.create_table(
        "workflow_steps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("step_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_runs.workflow_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
