"""Align the populated Postgres schema with the application's core ORM types."""

import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261006_align_postgres_core"
down_revision: Union[str, None] = "20260925_sezioni_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


_CORE_TIMESTAMPS = ("visura_requests", "visura_responses")
_FOREIGN_KEYS = (
    ("visura_requests", "location_id", "cadastral_locations", "id", "fk_visura_requests_location"),
    ("visura_properties", "location_id", "cadastral_locations", "id", "fk_visura_properties_location"),
    ("visura_properties", "subject_id", "cadastral_subjects", "id", "fk_visura_properties_subject"),
    ("visura_owners", "subject_id", "cadastral_subjects", "id", "fk_visura_owners_subject"),
    ("visura_owners", "right_id", "ownership_rights", "id", "fk_visura_owners_right"),
)
_CORE_TABLES = ("visura_requests", "visura_responses")


def _column(bind, table_name: str, column_name: str):
    for column in sa.inspect(bind).get_columns(table_name):
        if column["name"] == column_name:
            return column
    return None


def _align_created_at(bind, table_name: str) -> None:
    column = _column(bind, table_name, "created_at")
    if column is None or (
        isinstance(column["type"], sa.DateTime) and column["type"].timezone
    ):
        return

    invalid = bind.execute(
        sa.text(
            f"SELECT count(*) FROM {table_name} "
            "WHERE created_at IS NULL OR btrim(created_at::text) = ''"
        )
    ).scalar_one()
    if invalid:
        raise RuntimeError(f"{table_name}.created_at has {invalid} empty values; repair them before migrating")

    op.execute(f"ALTER TABLE {table_name} ALTER COLUMN created_at DROP DEFAULT")
    op.execute(
        f"""ALTER TABLE {table_name} ALTER COLUMN created_at TYPE TIMESTAMPTZ USING (
            CASE
                WHEN created_at::text ~ '(Z|[+-][0-9]{{2}}(:?[0-9]{{2}})?)$'
                    THEN created_at::text::timestamptz
                ELSE created_at::text::timestamp AT TIME ZONE 'Europe/Rome'
            END
        )"""
    )
    op.execute(f"ALTER TABLE {table_name} ALTER COLUMN created_at SET DEFAULT now()")
    op.execute(f"ALTER TABLE {table_name} ALTER COLUMN created_at SET NOT NULL")


def _align_success_type(bind) -> None:
    column = _column(bind, "visura_responses", "success")
    if column is None or isinstance(column["type"], sa.Boolean):
        return
    op.execute("ALTER TABLE visura_responses ALTER COLUMN success DROP DEFAULT")
    op.execute(
        "ALTER TABLE visura_responses ALTER COLUMN success TYPE BOOLEAN "
        "USING CASE WHEN success IS NULL THEN NULL "
        "WHEN lower(btrim(success::text)) IN ('0', 'false', 'f', 'no', 'n') THEN FALSE ELSE TRUE END"
    )


def _add_core_constraints(bind) -> None:
    inspector = sa.inspect(bind)
    for table_name, local_column, remote_table, remote_column, constraint_name in _FOREIGN_KEYS:
        if not inspector.has_table(table_name) or not inspector.has_table(remote_table):
            continue
        foreign_keys = inspector.get_foreign_keys(table_name)
        if any(
            fk.get("constrained_columns") == [local_column]
            and fk.get("referred_table") == remote_table
            and fk.get("referred_columns") == [remote_column]
            for fk in foreign_keys
        ):
            continue
        orphan_count = bind.execute(
            sa.text(
                f"SELECT count(*) FROM {table_name} child LEFT JOIN {remote_table} parent "
                f"ON child.{local_column} = parent.{remote_column} "
                f"WHERE child.{local_column} IS NOT NULL AND parent.{remote_column} IS NULL"
            )
        ).scalar_one()
        if orphan_count:
            raise RuntimeError(
                f"Cannot add {constraint_name}: {table_name}.{local_column} has {orphan_count} orphan values"
            )
        op.create_foreign_key(
            constraint_name,
            table_name,
            remote_table,
            [local_column],
            [remote_column],
        )
        inspector = sa.inspect(bind)


def _suspend_dependent_views(bind):
    """Temporarily remove dependent views/materialized views for core type changes.

    PostgreSQL tracks these dependencies by column, so ALTER COLUMN TYPE fails
    while a view or materialized view references a column being aligned. Save
    their definitions, indexes, ownership, grants, and relevant options before
    dropping them, then restore them after the column types have changed.
    """
    dependent = bind.execute(
        sa.text(
            """WITH RECURSIVE dependent_views AS (
                SELECT v.oid, v.relkind, 1 AS depth
                FROM pg_depend d
                JOIN pg_rewrite r ON r.oid = d.objid
                JOIN pg_class v ON v.oid = r.ev_class
                WHERE d.refobjid IN (
                    to_regclass('visura_requests'),
                    to_regclass('visura_responses')
                )
                  AND v.relkind IN ('v', 'm')
                UNION
                SELECT child.oid, child.relkind, parent.depth + 1
                FROM dependent_views parent
                JOIN pg_depend d ON d.refobjid = parent.oid
                JOIN pg_rewrite r ON r.oid = d.objid
                JOIN pg_class child ON child.oid = r.ev_class
                WHERE child.relkind IN ('v', 'm')
                  AND child.oid <> parent.oid
            )
            SELECT oid, relkind, max(depth) AS depth
            FROM dependent_views
            GROUP BY oid, relkind"""
        )
    ).mappings().all()
    views = []
    for row in dependent:
        view = bind.execute(
            sa.text(
                """SELECT n.nspname AS schema_name,
                          c.relname AS view_name,
                          c.relkind AS kind,
                          pg_get_viewdef(c.oid, true) AS definition,
                          pg_get_userbyid(c.relowner) AS owner,
                          c.reloptions AS options,
                          iv.check_option AS check_option,
                          ts.spcname AS tablespace
                   FROM pg_class c
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   LEFT JOIN pg_tablespace ts ON ts.oid = c.reltablespace
                   LEFT JOIN information_schema.views iv
                     ON iv.table_schema = n.nspname AND iv.table_name = c.relname
                   WHERE c.oid = :oid"""
            ),
            {"oid": row["oid"]},
        ).mappings().one()
        indexes = []
        if view["kind"] == "m":
            indexes = bind.execute(
                sa.text(
                    "SELECT pg_get_indexdef(indexrelid) FROM pg_index "
                    "WHERE indrelid = :oid ORDER BY indexrelid"
                ),
                {"oid": row["oid"]},
            ).scalars().all()
        grants = bind.execute(
            sa.text(
                """SELECT CASE WHEN acl.grantee = 0 THEN NULL
                                ELSE pg_get_userbyid(acl.grantee) END AS grantee,
                          acl.privilege_type,
                          acl.is_grantable
                   FROM pg_class c
                   CROSS JOIN LATERAL aclexplode(c.relacl) acl
                   WHERE c.oid = :oid"""
            ),
            {"oid": row["oid"]},
        ).mappings().all()
        views.append({**view, "depth": row["depth"], "grants": grants, "indexes": indexes})

    preparer = bind.dialect.identifier_preparer
    for view in sorted(views, key=lambda item: item["depth"], reverse=True):
        name = f"{preparer.quote(view['schema_name'])}.{preparer.quote(view['view_name'])}"
        relation_type = "MATERIALIZED VIEW" if view["kind"] == "m" else "VIEW"
        op.execute(f"DROP {relation_type} {name}")
    return views


def _restore_dependent_views(bind, views) -> None:
    preparer = bind.dialect.identifier_preparer
    for view in sorted(views, key=lambda item: item["depth"]):
        name = f"{preparer.quote(view['schema_name'])}.{preparer.quote(view['view_name'])}"
        options = []
        for option in view["options"] or []:
            key, _, value = option.partition("=")
            if not key.replace("_", "").replace(".", "").isalnum() or not value:
                raise RuntimeError(f"Unsupported PostgreSQL view option on {name}: {option}")
            options.append(f"{key}={value}")
        with_options = f" WITH ({', '.join(options)})" if options else ""
        if view["kind"] == "m":
            relation_type = "MATERIALIZED VIEW"
            tablespace = (
                f" TABLESPACE {preparer.quote(view['tablespace'])}"
                if view["tablespace"]
                else ""
            )
            definition = (
                f"CREATE {relation_type} {name}{with_options}{tablespace} "
                f"AS {_align_dependent_view_types(view['definition'])} WITH DATA"
            )
        else:
            relation_type = "VIEW"
            view_definition = _align_dependent_view_types(view["definition"])
            definition = f"CREATE {relation_type} {name}{with_options} AS {view_definition}"
            check_option = view["check_option"]
            if check_option in {"LOCAL", "CASCADED"}:
                definition += f" WITH {check_option} CHECK OPTION"
        op.execute(definition)
        for index_definition in view["indexes"]:
            op.execute(index_definition)
        op.execute(
            f"ALTER {relation_type} {name} OWNER TO {preparer.quote(view['owner'])}"
        )
        for grant in view["grants"]:
            privilege = grant["privilege_type"]
            if privilege not in {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}:
                raise RuntimeError(f"Unsupported PostgreSQL view privilege on {name}: {privilege}")
            grantee = "PUBLIC" if grant["grantee"] is None else preparer.quote(grant["grantee"])
            grant_option = " WITH GRANT OPTION" if grant["is_grantable"] else ""
            op.execute(f"GRANT {privilege} ON {name} TO {grantee}{grant_option}")


def _align_dependent_view_types(definition: str) -> str:
    """Update typed NULLs in saved UNION branches to match aligned core fields."""
    definition = definition.strip().removesuffix(";")
    for alias in ("request_created_at", "response_created_at"):
        definition = re.sub(
            rf"NULL\s*::\s*(?:text|character varying)\s+AS\s+{alias}\b",
            f"NULL::timestamptz AS {alias}",
            definition,
            flags=re.IGNORECASE,
        )
    definition = re.sub(
        r"NULL\s*::\s*(?:smallint|integer|bigint)\s+AS\s+response_success\b",
        "NULL::boolean AS response_success",
        definition,
        flags=re.IGNORECASE,
    )
    return definition


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This SISTER schema migration requires PostgreSQL")

    dependent_views = _suspend_dependent_views(bind)
    for table_name in _CORE_TIMESTAMPS:
        _align_created_at(bind, table_name)
    _align_success_type(bind)
    _add_core_constraints(bind)

    if sa.inspect(bind).has_table("cadastral_subjects"):
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_subject_fiscal_code "
            "ON cadastral_subjects (fiscal_code) WHERE fiscal_code IS NOT NULL"
        )
    _restore_dependent_views(bind, dependent_views)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This SISTER schema migration requires PostgreSQL")

    for table_name, local_column, _, _, constraint_name in reversed(_FOREIGN_KEYS):
        foreign_keys = sa.inspect(bind).get_foreign_keys(table_name) if sa.inspect(bind).has_table(table_name) else []
        if any(fk.get("name") == constraint_name for fk in foreign_keys):
            op.drop_constraint(constraint_name, table_name, type_="foreignkey")

    column = _column(bind, "visura_responses", "success")
    if column is not None and isinstance(column["type"], sa.Boolean):
        op.execute("ALTER TABLE visura_responses ALTER COLUMN success DROP DEFAULT")
        op.execute(
            "ALTER TABLE visura_responses ALTER COLUMN success TYPE BIGINT "
            "USING CASE WHEN success IS NULL THEN NULL WHEN success THEN 1 ELSE 0 END"
        )

    for table_name in _CORE_TIMESTAMPS:
        column = _column(bind, table_name, "created_at")
        if column is not None and isinstance(column["type"], sa.DateTime):
            op.execute(f"ALTER TABLE {table_name} ALTER COLUMN created_at DROP DEFAULT")
            op.execute(f"ALTER TABLE {table_name} ALTER COLUMN created_at TYPE TEXT USING created_at::text")
