"""Normalize ownership-right labels and catalog stable categorical values.

The ownership-right type/code/description triple describes a shared right
vocabulary entry. Share and dates describe an individual ownership record.
Other bounded source vocabularies use small natural-key tables with foreign
keys while retaining the existing readable values in their source columns.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261023_normalize_catalogs"
down_revision: Union[str, None] = "20261022_visura_polygons"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


ENUM_COLUMNS = (
    ("cadastral_subjects", "gender", "enum_gender"),
    ("mortgage_inspection_parties", "gender", "enum_gender"),
    ("cadastral_subjects", "subject_type", "enum_cadastral_subject_type"),
    ("cadastral_locations", "cadastre_type", "enum_cadastre_type"),
    ("property_groups", "cadastre_type", "enum_cadastre_type"),
    ("visura_responses", "cadastre_type", "enum_cadastre_type"),
    ("visura_requests", "request_type", "enum_visura_request_type"),
    ("visura_properties", "property_type", "enum_visura_property_type"),
    ("visura_properties", "category", "enum_visura_property_category"),
    ("visura_properties", "cadastral_class", "enum_visura_property_class"),
    ("visura_properties", "quality", "enum_visura_property_quality"),
    ("page_visit_form_elements", "element_type", "enum_page_visit_element_type"),
    ("visura_documents", "document_type", "enum_visura_document_type"),
    ("visura_documents", "file_format", "enum_visura_file_format"),
    ("building_classifications", "category", "enum_building_category"),
    ("building_classifications", "cadastral_class", "enum_building_class"),
    ("building_classifications", "consistency_unit", "enum_building_consistency_unit"),
    ("land_classifications", "cadastral_class", "enum_land_class"),
    ("land_classifications", "quality", "enum_land_quality"),
    ("mortgage_inspection_liens", "lien_type", "enum_mortgage_lien_type"),
    ("mortgage_inspection_parties", "quality", "enum_mortgage_party_quality"),
    ("mortgage_inspection_parties", "raw_quality", "enum_mortgage_party_raw_quality"),
    ("mortgage_inspection_parties", "role", "enum_mortgage_party_role"),
    ("mortgage_inspections", "note_type", "enum_mortgage_note_type"),
    ("mortgage_inspection_titles", "title_type", "enum_mortgage_title_type"),
    ("ownership_mutations", "ref_property_status", "enum_ownership_property_status"),
    ("activity_log", "source", "enum_activity_source"),
    ("activity_log", "action", "enum_activity_action"),
    ("activity_log", "status", "enum_activity_status"),
)

OWNER_VIEWS = (
    ("v_owners_by_fiscal_code", "rights", "owner"),
    ("v_sister_owner_context", "rw", "o"),
)


def _view_definition(bind, name: str) -> str | None:
    return bind.execute(
        sa.text("SELECT pg_get_viewdef(CAST(:view_name AS regclass), true)"),
        {"view_name": f"sister.{name}"},
    ).scalar_one_or_none()


def _capture_owner_views(bind) -> dict[str, str]:
    definitions = {}
    for view_name, _, _ in OWNER_VIEWS:
        definition = _view_definition(bind, view_name)
        if definition:
            definitions[view_name] = definition
    return definitions


def _restore_owner_views(definitions: dict[str, str], *, use_catalog: bool) -> None:
    for view_name, table_alias, owner_alias in OWNER_VIEWS:
        definition = definitions.get(view_name)
        if not definition:
            continue

        if use_catalog:
            for field_name in ("right_type", "right_code", "right_description"):
                definition = definition.replace(
                    f"{table_alias}.{field_name}", f"right_catalog.{field_name}"
                )
            owner_table = (
                "sister.ownership_rights"
                if "LEFT JOIN sister.ownership_rights " in definition
                else "ownership_rights"
            )
            catalog_table = (
                "sister.ownership_right_catalog"
                if "sister.ownership_rights " in owner_table
                else "ownership_right_catalog"
            )
            old_join = (
                f"LEFT JOIN {owner_table} {table_alias} ON "
                f"{table_alias}.id = {owner_alias}.right_id"
            )
            new_join = (
                f"{old_join} LEFT JOIN {catalog_table} right_catalog ON "
                f"right_catalog.id = {table_alias}.right_catalog_id"
            )
            if old_join not in definition:
                raise RuntimeError(
                    f"Could not update ownership-right join in sister.{view_name}; "
                    f"expected {old_join!r}; view text: {definition!r}"
                )
            definition = definition.replace(old_join, new_join, 1)
        else:
            for field_name in ("right_type", "right_code", "right_description"):
                definition = definition.replace(
                    f"right_catalog.{field_name}", f"{table_alias}.{field_name}"
                )
            catalog_table = (
                "sister.ownership_right_catalog"
                if "LEFT JOIN sister.ownership_right_catalog " in definition
                else "ownership_right_catalog"
            )
            catalog_join = (
                f"LEFT JOIN {catalog_table} right_catalog ON "
                f"right_catalog.id = {table_alias}.right_catalog_id"
            )
            if catalog_join not in definition:
                raise RuntimeError(f"Could not restore ownership-right join in sister.{view_name}")
            definition = definition.replace(catalog_join, "", 1)
        op.execute(f"CREATE OR REPLACE VIEW sister.{view_name} AS {definition}")


def _enum_names() -> list[str]:
    return list(dict.fromkeys(enum_name for _, _, enum_name in ENUM_COLUMNS))


def _enum_trigger_groups() -> dict[str, list[tuple[str, str]]]:
    grouped: dict[str, list[tuple[str, str]]] = {}
    for table_name, column_name, enum_name in ENUM_COLUMNS:
        grouped.setdefault(table_name, []).append((column_name, enum_name))
    return grouped


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This SISTER schema migration requires PostgreSQL")

    owner_views = _capture_owner_views(bind)

    op.execute(
        """CREATE TABLE sister.ownership_right_catalog (
            id BIGSERIAL PRIMARY KEY,
            right_type TEXT,
            right_code TEXT,
            right_description TEXT
        )"""
    )
    op.execute(
        """INSERT INTO sister.ownership_right_catalog
               (right_type, right_code, right_description)
           SELECT DISTINCT right_type, right_code, right_description
           FROM sister.ownership_rights"""
    )
    op.execute(
        """CREATE UNIQUE INDEX uq_ownership_right_catalog
           ON sister.ownership_right_catalog (right_type, right_code, right_description)
           NULLS NOT DISTINCT"""
    )
    op.execute("ALTER TABLE sister.ownership_rights ADD COLUMN right_catalog_id BIGINT")
    op.execute(
        """UPDATE sister.ownership_rights AS rights
           SET right_catalog_id = catalog.id
           FROM sister.ownership_right_catalog AS catalog
           WHERE catalog.right_type IS NOT DISTINCT FROM rights.right_type
             AND catalog.right_code IS NOT DISTINCT FROM rights.right_code
             AND catalog.right_description IS NOT DISTINCT FROM rights.right_description"""
    )
    op.execute("ALTER TABLE sister.ownership_rights ALTER COLUMN right_catalog_id SET NOT NULL")
    op.execute(
        """ALTER TABLE sister.ownership_rights
           ADD CONSTRAINT fk_ownership_rights_catalog
           FOREIGN KEY (right_catalog_id)
           REFERENCES sister.ownership_right_catalog(id)"""
    )
    op.execute(
        "CREATE INDEX ix_ownership_rights_right_catalog_id "
        "ON sister.ownership_rights (right_catalog_id)"
    )
    op.execute("ALTER TABLE sister.ownership_rights DROP CONSTRAINT IF EXISTS uq_ownership_right")
    op.execute("ALTER TABLE sister.ownership_rights DROP CONSTRAINT IF EXISTS uq_ownership_right_terms")
    op.execute(
        """ALTER TABLE sister.ownership_rights
           ADD CONSTRAINT uq_ownership_right_terms
           UNIQUE (right_catalog_id, ownership_share, start_date, end_date)"""
    )
    _restore_owner_views(owner_views, use_catalog=True)
    op.execute(
        """ALTER TABLE sister.ownership_rights
           DROP COLUMN right_type,
           DROP COLUMN right_code,
           DROP COLUMN right_description"""
    )

    for enum_name in _enum_names():
        op.execute(f"CREATE TABLE sister.{enum_name} (value TEXT PRIMARY KEY)")

    for table_name, column_name, enum_name in ENUM_COLUMNS:
        op.execute(
            f"""INSERT INTO sister.{enum_name} (value)
                SELECT DISTINCT {column_name}::text
                FROM sister.{table_name}
                WHERE {column_name} IS NOT NULL
                ON CONFLICT (value) DO NOTHING"""
        )

    op.execute(
        """CREATE OR REPLACE FUNCTION sister.ensure_enum_reference_value()
           RETURNS trigger
           LANGUAGE plpgsql
           SECURITY DEFINER
           SET search_path = pg_catalog, sister
           AS $$
           DECLARE
               pair_index INTEGER;
               source_column TEXT;
               enum_table TEXT;
               source_value TEXT;
           BEGIN
               FOR pair_index IN 0..(TG_NARGS / 2 - 1) LOOP
                   source_column := TG_ARGV[pair_index * 2];
                   enum_table := TG_ARGV[pair_index * 2 + 1];
                   source_value := to_jsonb(NEW) ->> source_column;
                   IF source_value IS NOT NULL THEN
                       EXECUTE format(
                           'INSERT INTO sister.%I (value) VALUES ($1) ON CONFLICT (value) DO NOTHING',
                           enum_table
                       ) USING source_value;
                   END IF;
               END LOOP;
               RETURN NEW;
           END;
           $$"""
    )

    for table_name, enum_fields in _enum_trigger_groups().items():
        args = ", ".join(
            "'{}', '{}'".format(column_name, enum_name)
            for column_name, enum_name in enum_fields
        )
        trigger_name = f"trg_enum_catalog_{table_name}"
        op.execute(
            f"""CREATE TRIGGER {trigger_name}
                BEFORE INSERT OR UPDATE ON sister.{table_name}
                FOR EACH ROW EXECUTE FUNCTION sister.ensure_enum_reference_value({args})"""
        )

    for table_name, column_name, enum_name in ENUM_COLUMNS:
        constraint_name = f"fk_{table_name}_{column_name}_enum"
        op.execute(
            f"""ALTER TABLE sister.{table_name}
                ADD CONSTRAINT {constraint_name}
                FOREIGN KEY ({column_name}) REFERENCES sister.{enum_name}(value) NOT VALID"""
        )
        op.execute(
            f"ALTER TABLE sister.{table_name} VALIDATE CONSTRAINT {constraint_name}"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This SISTER schema migration requires PostgreSQL")

    owner_views = _capture_owner_views(bind)

    for table_name, column_name, _ in ENUM_COLUMNS:
        constraint_name = f"fk_{table_name}_{column_name}_enum"
        op.execute(
            f"ALTER TABLE sister.{table_name} DROP CONSTRAINT IF EXISTS {constraint_name}"
        )
    for table_name in _enum_trigger_groups():
        trigger_name = f"trg_enum_catalog_{table_name}"
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON sister.{table_name}")
    op.execute("DROP FUNCTION IF EXISTS sister.ensure_enum_reference_value()")
    for enum_name in reversed(_enum_names()):
        op.execute(f"DROP TABLE IF EXISTS sister.{enum_name}")

    op.execute("ALTER TABLE sister.ownership_rights DROP CONSTRAINT IF EXISTS uq_ownership_right_terms")
    op.execute("ALTER TABLE sister.ownership_rights DROP CONSTRAINT IF EXISTS fk_ownership_rights_catalog")
    op.execute("DROP INDEX IF EXISTS sister.ix_ownership_rights_right_catalog_id")
    op.execute(
        """ALTER TABLE sister.ownership_rights
           ADD COLUMN right_type TEXT,
           ADD COLUMN right_code TEXT,
           ADD COLUMN right_description TEXT"""
    )
    op.execute(
        """UPDATE sister.ownership_rights AS rights
           SET right_type = catalog.right_type,
               right_code = catalog.right_code,
               right_description = catalog.right_description
           FROM sister.ownership_right_catalog AS catalog
           WHERE catalog.id = rights.right_catalog_id"""
    )
    _restore_owner_views(owner_views, use_catalog=False)
    op.execute("ALTER TABLE sister.ownership_rights DROP COLUMN right_catalog_id")
    op.execute("DROP TABLE sister.ownership_right_catalog")
    op.execute(
        """ALTER TABLE sister.ownership_rights
           ADD CONSTRAINT uq_ownership_right
           UNIQUE (right_type, right_code, right_description,
                   ownership_share, start_date, end_date)"""
    )
