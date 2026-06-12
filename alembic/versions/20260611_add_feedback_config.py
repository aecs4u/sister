"""add feedback_config and feedback_unsubscribes tables

Revision ID: 20260611_add_feedback_config
Revises: 20260608_add_visura_subtype
Create Date: 2026-06-11
"""

from alembic import op
import sqlalchemy as sa

revision: str = "20260611_add_feedback_config"
down_revision: str = "20260608_add_visura_subtype"
branch_labels = None
depends_on = None

_DEFAULT_CTA = "Lascia il tuo feedback →"
_DEFAULT_UNSUB = "Non vuoi più ricevere queste email?"


def upgrade() -> None:
    op.create_table(
        "feedback_config",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("cc_emails", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("bcc_emails", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("invitation_subject", sa.Text, nullable=False, server_default="Il tuo feedback è importante"),
        sa.Column("invitation_intro", sa.Text, nullable=False, server_default=""),
        sa.Column("invitation_bullets", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("invitation_cta_text", sa.Text, nullable=False, server_default=_DEFAULT_CTA),
        sa.Column("invitation_privacy_note", sa.Text, nullable=False, server_default=""),
        sa.Column("invitation_signature", sa.Text, nullable=False, server_default=""),
        sa.Column("invitation_unsub_text", sa.Text, nullable=False, server_default=_DEFAULT_UNSUB),
        sa.Column("invitation_unsub_link_text", sa.Text, nullable=False, server_default="Disiscriviti qui"),
        sa.Column("grace_period_days", sa.Integer, nullable=False, server_default="30"),
    )
    op.create_table(
        "feedback_unsubscribes",
        sa.Column("email", sa.String, primary_key=True),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("feedback_unsubscribes")
    op.drop_table("feedback_config")
