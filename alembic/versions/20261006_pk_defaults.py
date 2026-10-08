"""Restore generated defaults for integer primary keys in the populated schema."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261006_pk_defaults"
down_revision: Union[str, None] = "20261006_prefix_views"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


_SEQUENCE_PREFIX = "sister_pkseq_20261006_"


def _integer_id_primary_keys(bind):
    inspector = sa.inspect(bind)
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    preparer = bind.dialect.identifier_preparer

    for table_name in inspector.get_table_names(schema=schema):
        columns = {
            column["name"]: column
            for column in inspector.get_columns(table_name, schema=schema)
        }
        column = columns.get("id")
        if column is None or getattr(column["type"], "_type_affinity", None) is not sa.Integer:
            continue
        if inspector.get_pk_constraint(table_name, schema=schema).get("constrained_columns") != ["id"]:
            continue
        if any(
            "id" in fk.get("constrained_columns", [])
            for fk in inspector.get_foreign_keys(table_name, schema=schema)
        ):
            continue

        sequence_name = f"{_SEQUENCE_PREFIX}{table_name}"
        quoted_table = f"{preparer.quote_schema(schema)}.{preparer.quote(table_name)}"
        quoted_sequence = f"{preparer.quote_schema(schema)}.{preparer.quote(sequence_name)}"
        sequence_literal = quoted_sequence.replace("'", "''")
        yield table_name, column, quoted_table, sequence_name, quoted_sequence, sequence_literal


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This SISTER schema migration requires PostgreSQL")

    schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    for (
        _,
        column,
        quoted_table,
        sequence_name,
        quoted_sequence,
        sequence_literal,
    ) in _integer_id_primary_keys(bind):
        if column.get("default") is not None or column.get("identity"):
            continue

        op.execute(
            sa.schema.CreateSequence(
                sa.Sequence(sequence_name, schema=schema),
                if_not_exists=True,
            )
        )
        op.execute(
            f"ALTER TABLE {quoted_table} ALTER COLUMN {bind.dialect.identifier_preparer.quote('id')} "
            f"SET DEFAULT nextval('{sequence_literal}'::regclass)"
        )
        op.execute(f"ALTER SEQUENCE {quoted_sequence} OWNED BY {quoted_table}.id")
        bind.execute(
            sa.text(
                f"SELECT setval(CAST(:sequence_name AS regclass), "
                f"COALESCE(MAX({bind.dialect.identifier_preparer.quote('id')}), 1), "
                f"MAX({bind.dialect.identifier_preparer.quote('id')}) IS NOT NULL) "
                f"FROM {quoted_table}"
            ),
            {"sequence_name": quoted_sequence},
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This SISTER schema migration requires PostgreSQL")

    preparer = bind.dialect.identifier_preparer
    for (
        _,
        column,
        quoted_table,
        sequence_name,
        quoted_sequence,
        _,
    ) in _integer_id_primary_keys(bind):
        if sequence_name not in (column.get("default") or ""):
            continue
        op.execute(
            f"ALTER TABLE {quoted_table} ALTER COLUMN {preparer.quote('id')} DROP DEFAULT"
        )
        op.execute(f"DROP SEQUENCE IF EXISTS {quoted_sequence}")
