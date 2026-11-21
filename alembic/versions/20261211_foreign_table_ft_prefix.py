"""Prefix every foreign table name with ft_."""

import sqlalchemy as sa
from alembic import op

revision = "20261211_foreign_table_ft_prefix"
down_revision = "20261210_aecs4u_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    preparer = bind.dialect.identifier_preparer
    foreign_tables = bind.execute(
        sa.text(
            "SELECT n.nspname, c.relname "
            "FROM pg_foreign_table f "
            "JOIN pg_class c ON c.oid = f.ftrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname <> 'information_schema' "
            "AND left(n.nspname, 3) <> 'pg_' "
            "AND left(c.relname, 3) <> 'ft_' "
            "ORDER BY n.nspname, c.relname"
        )
    ).all()
    for schema, old_name in foreign_tables:
        new_name = f"ft_{old_name}"
        collision = bind.execute(
            sa.text(
                "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND c.relname = :name"
            ),
            {"schema": schema, "name": new_name},
        ).first()
        if collision:
            raise RuntimeError(f"Cannot rename foreign table in {schema}: target name already exists")
        bind.exec_driver_sql(
            "ALTER FOREIGN TABLE "
            f"{preparer.quote_schema(schema)}.{preparer.quote(old_name)} "
            f"RENAME TO {preparer.quote(new_name)}"
        )


def downgrade() -> None:
    raise RuntimeError("Foreign-table prefix changes require an explicit, collision-checked manual downgrade")
