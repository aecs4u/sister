"""Store the "Elenco Province" table of a subject search (provinces with assets, buildings and land counts)."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261101_request_province"
down_revision: Union[str, None] = "20261031_document_favorites"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    import os

    # the test suite builds a throwaway schema and passes its name as the ``sister_schema`` option
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("request_province", schema=schema):
        op.create_table(
            "request_province",
            sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
            sa.Column("request_id", sa.BigInteger(), nullable=False),
            sa.Column("province_index", sa.Integer(), nullable=False),
            sa.Column("subject_id", sa.BigInteger(), nullable=True),
            sa.Column("fiscal_code", sa.String(), nullable=True),
            sa.Column("province_code", sa.String(), nullable=True),
            sa.Column("province_name", sa.String(), nullable=False),
            sa.Column("buildings_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("land_parcels_count", sa.Integer(), server_default="0", nullable=False),
            sa.ForeignKeyConstraint(["request_id"], [f"{schema}.request.id"], name="request_province_request_id_fkey"),
            sa.ForeignKeyConstraint(["subject_id"], [f"{schema}.subject.id"], name="request_province_subject_id_fkey"),
            sa.PrimaryKeyConstraint("id", name="request_province_pkey"),
            sa.UniqueConstraint("request_id", "province_index", name="uq_request_province"),
            sa.CheckConstraint("province_code ~ '^[A-Z]{2}$'", name="ck_request_province_code"),
            schema=schema,
        )
        op.create_index("ix_request_province_subject", "request_province", ["subject_id"], schema=schema)
        op.create_index("ix_request_province_fiscal_code", "request_province", ["fiscal_code"], schema=schema)


def downgrade() -> None:
    op.drop_table("request_province", schema=_schema())
