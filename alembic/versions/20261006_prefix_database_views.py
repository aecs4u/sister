"""Rename SISTER database views to the v_ prefix convention."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261006_prefix_views"
down_revision: Union[str, None] = "20261006_align_postgres_core"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _rename_views(*, downgrade: bool = False) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This SISTER schema migration requires PostgreSQL")

    if downgrade:
        direction = "prefix"
        target_expression = "'sister_' || substr(c.relname, 10) || '_v'"
    else:
        direction = "suffix"
        target_expression = "'v_' || substr(c.relname, 1, length(c.relname) - 2)"

    views = bind.execute(
        sa.text(
            f"""SELECT n.nspname AS schema_name,
                       c.relname AS relation_name,
                       c.relkind AS relation_kind,
                       {target_expression} AS target_name,
                       target.oid IS NOT NULL AS target_exists
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_class target
                  ON target.relnamespace = c.relnamespace
                 AND target.relname = {target_expression}
                WHERE c.relkind IN ('v', 'm')
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND CASE :direction
                        WHEN 'suffix' THEN left(c.relname, 7) = 'sister_'
                                          AND right(c.relname, 2) = '_v'
                                          AND length(c.relname) > 9
                        WHEN 'prefix' THEN left(c.relname, 9) = 'v_sister_'
                                          AND length(c.relname) > 9
                      END
                ORDER BY n.nspname, c.relname"""
        ),
        {"direction": direction},
    ).mappings().all()

    preparer = bind.dialect.identifier_preparer
    for view in views:
        old_name = view["relation_name"]
        new_name = view["target_name"]
        qualified_name = (
            f"{preparer.quote(view['schema_name'])}.{preparer.quote(old_name)}"
        )
        if view["target_exists"]:
            raise RuntimeError(
                f"Cannot rename database view {qualified_name}: "
                f"{view['schema_name']}.{new_name} already exists"
            )

        relation_type = "MATERIALIZED VIEW" if view["relation_kind"] == "m" else "VIEW"
        op.execute(
            f"ALTER {relation_type} {qualified_name} "
            f"RENAME TO {preparer.quote(new_name)}"
        )


def upgrade() -> None:
    _rename_views()


def downgrade() -> None:
    _rename_views(downgrade=True)
