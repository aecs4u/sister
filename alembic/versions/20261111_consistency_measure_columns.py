"""Store cadastral consistency in separate square-metre and room columns."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261111_consistency_measure_columns"
down_revision: Union[str, None] = "20261110_property_omi_selection"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


TABLES = ("building_unit", "property")
NEW_COLUMNS = ("consistency_sqm", "consistency_rooms")


def _schema() -> str:
    import os

    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table_name in TABLES:
        if not inspector.has_table(table_name, schema=schema):
            continue
        existing = {column["name"] for column in inspector.get_columns(table_name, schema=schema)}
        for name in NEW_COLUMNS:
            if name not in existing:
                op.add_column(table_name, sa.Column(name, sa.Numeric(), nullable=True), schema=schema)

        if {"consistency_value", "consistency_unit"}.issubset(existing):
            table = sa.table(
                table_name,
                sa.column("consistency_value", sa.Numeric()),
                sa.column("consistency_unit", sa.String()),
                sa.column("consistency_sqm", sa.Numeric()),
                sa.column("consistency_rooms", sa.Numeric()),
                schema=schema,
            )
            unit = sa.func.upper(sa.func.trim(table.c.consistency_unit))
            op.execute(
                table.update()
                .where(
                    table.c.consistency_sqm.is_(None),
                    table.c.consistency_value.is_not(None),
                    unit.in_(("MQ", "M2", "M²")),
                )
                .values(consistency_sqm=table.c.consistency_value)
            )
            op.execute(
                table.update()
                .where(
                    table.c.consistency_rooms.is_(None),
                    table.c.consistency_value.is_not(None),
                    unit.in_(("VANI", "VANO")),
                )
                .values(consistency_rooms=table.c.consistency_value)
            )


def downgrade() -> None:
    schema = _schema()
    inspector = sa.inspect(op.get_bind())
    for table_name in TABLES:
        if not inspector.has_table(table_name, schema=schema):
            continue
        existing = {column["name"] for column in inspector.get_columns(table_name, schema=schema)}
        for name in reversed(NEW_COLUMNS):
            if name in existing:
                op.drop_column(table_name, name, schema=schema)
