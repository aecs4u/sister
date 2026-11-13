"""Normalize profile and asset identities, province holdings, and calculation payloads."""

import os

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op


revision = "20261203_schema_identity_flatness"
down_revision = "20261202_pagopa_payment_history"
branch_labels = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def _archive_schema() -> str:
    return os.getenv("DATABASE_ARCHIVE_SCHEMA", "sister_archive")


def _has_constraint(inspector, table: str, schema: str, name: str) -> bool:
    return any(c.get("name") == name for c in inspector.get_unique_constraints(table, schema=schema)) or any(
        c.get("name") == name for c in inspector.get_foreign_keys(table, schema=schema)
    ) or any(c.get("name") == name for c in inspector.get_check_constraints(table, schema=schema))


def upgrade() -> None:
    schema = _schema()
    archive = _archive_schema()
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # A profile identity exists independently of any particular user's notes or saved searches.
    op.create_table(
        "subject_profile",
        sa.Column("subject_type", sa.String(16), nullable=False),
        sa.Column("subject_code", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("subject_type IN ('company', 'person')", name="ck_subject_profile_type"),
        sa.PrimaryKeyConstraint("subject_type", "subject_code", name="pk_subject_profile"),
        schema=schema,
    )
    op.execute(sa.text(f"""
        INSERT INTO {schema}.subject_profile (subject_type, subject_code)
        SELECT subject_type, subject_code FROM {schema}.subject_favorite
        UNION SELECT subject_type, subject_code FROM {schema}.subject_note
        UNION SELECT subject_type, subject_code FROM {schema}.subject_tag
        UNION SELECT subject_type, subject_code FROM {schema}.subject_access
        UNION SELECT subject_type, subject_code FROM {schema}.subject_purchase
        ON CONFLICT DO NOTHING
    """))
    for table in ("subject_favorite", "subject_note", "subject_tag", "subject_access", "subject_purchase"):
        name = f"fk_{table}_profile"
        if not _has_constraint(inspector, table, schema, name):
            op.create_foreign_key(
                name, table, "subject_profile", ["subject_type", "subject_code"], ["subject_type", "subject_code"],
                source_schema=schema, referent_schema=schema,
            )

    # Existing SHA-256 keys cannot be inverted. Keep them as version 0 until an identity-bearing UI write resolves them.
    op.create_table(
        "property_asset",
        sa.Column("asset_key", sa.String(64), nullable=False),
        sa.Column("identity_version", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("cadastre_type", sa.String(), nullable=True),
        sa.Column("municipality_code", sa.String(), nullable=True),
        sa.Column("municipality", sa.String(), nullable=True),
        sa.Column("province", sa.String(), nullable=True),
        sa.Column("section", sa.String(), nullable=True),
        sa.Column("sheet", sa.String(), nullable=True),
        sa.Column("parcel", sa.String(), nullable=True),
        sa.Column("subunit", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("asset_key ~ '^[a-f0-9]{64}$'", name="ck_property_asset_key_sha256"),
        sa.CheckConstraint(
            "(identity_version = 0 AND cadastre_type IS NULL AND municipality_code IS NULL AND municipality IS NULL "
            "AND province IS NULL AND section IS NULL AND sheet IS NULL AND parcel IS NULL AND subunit IS NULL) OR "
            "(identity_version = 1 AND cadastre_type IS NOT NULL AND sheet IS NOT NULL AND parcel IS NOT NULL "
            "AND (municipality_code IS NOT NULL OR municipality IS NOT NULL))",
            name="ck_property_asset_identity_version",
        ),
        sa.PrimaryKeyConstraint("asset_key", name="pk_property_asset"),
        schema=schema,
    )
    settings = (
        "manual_property_valuation", "property_valuation_preference", "property_affrancation_amount",
        "property_omi_override", "property_omi_selection", "property_actual_size",
    )
    union = " UNION ".join(f"SELECT asset_key FROM {schema}.{table}" for table in settings)
    op.execute(sa.text(f"INSERT INTO {schema}.property_asset (asset_key) {union} ON CONFLICT DO NOTHING"))
    for table in settings:
        name = f"fk_{table}_asset"
        if not _has_constraint(inspector, table, schema, name):
            op.create_foreign_key(
                name, table, "property_asset", ["asset_key"], ["asset_key"],
                source_schema=schema, referent_schema=schema,
            )

    # Replace the comma-separated province list with one row per holding and province.
    op.create_table(
        "subject_holding_province",
        sa.Column("fiscal_code", sa.String(16), nullable=False),
        sa.Column("province", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["fiscal_code"], [f"{schema}.subject_holding.fiscal_code"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("fiscal_code", "province", name="pk_subject_holding_province"),
        schema=schema,
    )
    op.execute(sa.text(f"""
        INSERT INTO {schema}.subject_holding_province (fiscal_code, province)
        SELECT h.fiscal_code, btrim(p.province)
        FROM {schema}.subject_holding h
        CROSS JOIN LATERAL regexp_split_to_table(coalesce(h.provinces, ''), '\\s*,\\s*') AS p(province)
        WHERE btrim(p.province) <> ''
        ON CONFLICT DO NOTHING
    """))
    if "provinces" in {column["name"] for column in inspector.get_columns("subject_holding", schema=schema)}:
        op.drop_column("subject_holding", "provinces", schema=schema)
    if "province_count" in {column["name"] for column in inspector.get_columns("subject_holding", schema=schema)}:
        op.drop_column("subject_holding", "province_count", schema=schema)

    # Parent keys include request/subject identity so a row cannot point across two parents.
    op.create_unique_constraint("uq_request_result_id_request", "request_result", ["id", "request_id"], schema=schema)
    op.create_unique_constraint("uq_subject_record_id_subject", "subject_record", ["id", "subject_id"], schema=schema)
    mismatch = bind.execute(sa.text(f"""
        SELECT 'property' AS table_name, count(*) AS n FROM {schema}.property p
        JOIN {schema}.request_result r ON r.id = p.result_id WHERE r.request_id <> p.request_id
        UNION ALL
        SELECT 'result_owner', count(*) FROM {schema}.result_owner o
        JOIN {schema}.request_result r ON r.id = o.result_id WHERE r.request_id <> o.request_id
        UNION ALL
        SELECT 'property_subject_record', count(*) FROM {schema}.property p
        JOIN {schema}.subject_record sr ON sr.id = p.subject_record_id
        WHERE p.subject_id IS NOT NULL AND sr.subject_id IS DISTINCT FROM p.subject_id
        UNION ALL
        SELECT 'result_owner_subject_record', count(*) FROM {schema}.result_owner o
        JOIN {schema}.subject_record sr ON sr.id = o.subject_record_id
        WHERE o.subject_id IS NOT NULL AND sr.subject_id IS DISTINCT FROM o.subject_id
    """)).all()
    mismatches = {str(table): int(count) for table, count in mismatch if count}
    if mismatches:
        raise RuntimeError(f"Cannot add composite parent constraints; mismatched rows: {mismatches}")
    op.create_foreign_key(
        "fk_property_result_request", "property", "request_result", ["result_id", "request_id"], ["id", "request_id"],
        source_schema=schema, referent_schema=schema,
    )
    op.create_foreign_key(
        "fk_result_owner_result_request", "result_owner", "request_result", ["result_id", "request_id"], ["id", "request_id"],
        source_schema=schema, referent_schema=schema,
    )
    op.create_foreign_key(
        "fk_property_subject_record_subject", "property", "subject_record", ["subject_record_id", "subject_id"],
        ["id", "subject_id"], source_schema=schema, referent_schema=schema,
    )
    op.create_foreign_key(
        "fk_result_owner_subject_record_subject", "result_owner", "subject_record", ["subject_record_id", "subject_id"],
        ["id", "subject_id"], source_schema=schema, referent_schema=schema,
    )

    # The snapshot fingerprint stays queryable in sister; structured result payload moves to sister_archive.
    op.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{archive}"'))
    op.create_table(
        "calculation_snapshot_payload",
        sa.Column("scope_type", sa.String(24), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("viewer_key", sa.Text(), nullable=False),
        sa.Column("calculation_type", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.ForeignKeyConstraint(
            ["scope_type", "scope_id", "viewer_key", "calculation_type"],
            [f"{schema}.calculation_snapshot.scope_type", f"{schema}.calculation_snapshot.scope_id",
             f"{schema}.calculation_snapshot.viewer_key", f"{schema}.calculation_snapshot.calculation_type"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("scope_type", "scope_id", "viewer_key", "calculation_type",
                                name="pk_calculation_snapshot_payload"),
        schema=archive,
    )
    op.execute(sa.text(f"""
        INSERT INTO {archive}.calculation_snapshot_payload
            (scope_type, scope_id, viewer_key, calculation_type, result)
        SELECT scope_type, scope_id, viewer_key, calculation_type, result FROM {schema}.calculation_snapshot
    """))
    if "result" in {column["name"] for column in inspector.get_columns("calculation_snapshot", schema=schema)}:
        op.drop_column("calculation_snapshot", "result", schema=schema)


def downgrade() -> None:
    schema = _schema()
    archive = _archive_schema()
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("calculation_snapshot_payload", schema=archive):
        op.add_column("calculation_snapshot", sa.Column("result", postgresql.JSONB(), nullable=True), schema=schema)
        op.execute(sa.text(f"""
            UPDATE {schema}.calculation_snapshot s SET result = p.result
            FROM {archive}.calculation_snapshot_payload p
            WHERE (s.scope_type, s.scope_id, s.viewer_key, s.calculation_type) =
                  (p.scope_type, p.scope_id, p.viewer_key, p.calculation_type)
        """))
        op.alter_column("calculation_snapshot", "result", nullable=False, schema=schema)
        op.drop_table("calculation_snapshot_payload", schema=archive)

    for table, name in (
        ("result_owner", "fk_result_owner_subject_record_subject"),
        ("property", "fk_property_subject_record_subject"),
        ("result_owner", "fk_result_owner_result_request"),
        ("property", "fk_property_result_request"),
    ):
        op.drop_constraint(name, table, schema=schema, type_="foreignkey")
    op.drop_constraint("uq_subject_record_id_subject", "subject_record", schema=schema, type_="unique")
    op.drop_constraint("uq_request_result_id_request", "request_result", schema=schema, type_="unique")

    if inspector.has_table("subject_holding_province", schema=schema):
        op.add_column("subject_holding", sa.Column("provinces", sa.String(), nullable=True), schema=schema)
        op.add_column("subject_holding", sa.Column("province_count", sa.Integer(), nullable=True), schema=schema)
        op.execute(sa.text(f"""
            UPDATE {schema}.subject_holding h SET
                provinces = x.provinces,
                province_count = x.province_count
            FROM (
                SELECT fiscal_code, string_agg(province, ', ' ORDER BY province) AS provinces, count(*)::int AS province_count
                FROM {schema}.subject_holding_province GROUP BY fiscal_code
            ) x WHERE x.fiscal_code = h.fiscal_code
        """))
        op.drop_table("subject_holding_province", schema=schema)

    for table in reversed((
        "manual_property_valuation", "property_valuation_preference", "property_affrancation_amount",
        "property_omi_override", "property_omi_selection", "property_actual_size",
    )):
        op.drop_constraint(f"fk_{table}_asset", table, schema=schema, type_="foreignkey")
    op.drop_table("property_asset", schema=schema)

    for table in ("subject_purchase", "subject_access", "subject_tag", "subject_note", "subject_favorite"):
        op.drop_constraint(f"fk_{table}_profile", table, schema=schema, type_="foreignkey")
    op.drop_table("subject_profile", schema=schema)
