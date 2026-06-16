"""normalize location into cadastral_locations; split visura_documents schema

Revision ID: 20260616_normalize_location
Revises: 20260608_add_situazione_al
Create Date: 2026-06-16
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260616_normalize_location"
down_revision: Union[str, None] = "20260608_add_situazione_al"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    ).fetchone()
    return bool(row and row[0])


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. cadastral_locations (may already exist from init_db) ───────────────
    if not _table_exists("cadastral_locations"):
        op.create_table(
            "cadastral_locations",
            sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
            sa.Column("cadastre_type", sa.String(), nullable=False, server_default=""),
            sa.Column("province", sa.String(), nullable=False, server_default=""),
            sa.Column("municipality", sa.String(), nullable=False, server_default=""),
            sa.Column("sheet", sa.String(), nullable=False, server_default=""),
            sa.Column("parcel", sa.String(), nullable=False, server_default=""),
            sa.Column("subunit", sa.String(), nullable=False, server_default=""),
            sa.Column("section", sa.String(), nullable=False, server_default=""),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "cadastre_type", "province", "municipality", "sheet", "parcel", "subunit", "section",
                name="uq_location",
            ),
        )
        op.create_index("idx_location_lookup", "cadastral_locations", ["province", "municipality", "sheet", "parcel"])

    # ── 2. document_metadata (may already exist from init_db) ─────────────────
    if not _table_exists("document_metadata"):
        op.create_table(
            "document_metadata",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("location_id", sa.Integer(), nullable=True),
            sa.Column("municipality_code", sa.String(), nullable=True),
            sa.Column("view_subtype", sa.String(), nullable=True),
            sa.Column("protocol", sa.String(), nullable=True),
            sa.Column("year", sa.String(), nullable=True),
            sa.Column("title", sa.String(), nullable=True),
            sa.Column("reference_date", sa.String(), nullable=True),
            sa.Column("registry_view_type", sa.String(), nullable=True),
            sa.Column("service_type", sa.String(), nullable=True),
            sa.Column("generation_date", sa.String(), nullable=True),
            sa.Column("generation_time", sa.String(), nullable=True),
            sa.Column("source_system", sa.String(), nullable=True),
            sa.Column("liquidation_protocol", sa.String(), nullable=True),
            sa.Column("liquidation_year", sa.String(), nullable=True),
            sa.Column("liquidation_units", sa.Integer(), nullable=True),
            sa.Column("requester", sa.String(), nullable=True),
            sa.Column("historical_total_area", sa.Numeric(), nullable=True),
            sa.Column("historical_excluded_area", sa.Numeric(), nullable=True),
            sa.Column("content", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["id"], ["visura_documents.id"]),
            sa.ForeignKeyConstraint(["location_id"], ["cadastral_locations.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("idx_document_metadata_location", "document_metadata", ["location_id"])

    # ── 3. visura_documents: add subject + requested_at (ORM renamed old cols) ─
    if not _column_exists("visura_documents", "subject"):
        op.add_column("visura_documents", sa.Column("subject", sa.String(), nullable=True))
        if _column_exists("visura_documents", "oggetto"):
            conn.execute(sa.text("UPDATE visura_documents SET subject = oggetto WHERE subject IS NULL"))

    if not _column_exists("visura_documents", "requested_at"):
        op.add_column("visura_documents", sa.Column("requested_at", sa.String(), nullable=True))
        if _column_exists("visura_documents", "richiesta_del"):
            conn.execute(sa.text("UPDATE visura_documents SET requested_at = richiesta_del WHERE requested_at IS NULL"))

    # ── 4. visura_requests: add location_id ───────────────────────────────────
    if not _column_exists("visura_requests", "location_id"):
        op.add_column("visura_requests", sa.Column("location_id", sa.Integer(), nullable=True))
        op.create_index("ix_visura_requests_location_id", "visura_requests", ["location_id"])
        # Populate from old inline location columns (if they exist)
        if _column_exists("visura_requests", "tipo_catasto"):
            conn.execute(sa.text("""
                INSERT OR IGNORE INTO cadastral_locations
                    (cadastre_type, province, municipality, sheet, parcel, subunit, section)
                SELECT
                    COALESCE(tipo_catasto, ''),
                    COALESCE(provincia, ''),
                    COALESCE(comune, ''),
                    COALESCE(foglio, ''),
                    COALESCE(particella, ''),
                    COALESCE(subalterno, ''),
                    COALESCE(sezione, '')
                FROM visura_requests
            """))
            conn.execute(sa.text("""
                UPDATE visura_requests SET location_id = (
                    SELECT id FROM cadastral_locations
                    WHERE cadastre_type = COALESCE(visura_requests.tipo_catasto, '')
                      AND province      = COALESCE(visura_requests.provincia, '')
                      AND municipality  = COALESCE(visura_requests.comune, '')
                      AND sheet         = COALESCE(visura_requests.foglio, '')
                      AND parcel        = COALESCE(visura_requests.particella, '')
                      AND subunit       = COALESCE(visura_requests.subalterno, '')
                      AND section       = COALESCE(visura_requests.sezione, '')
                )
                WHERE location_id IS NULL
            """))

    # ── 5. visura_properties: add location_id + property_type ─────────────────
    if not _column_exists("visura_properties", "property_type"):
        op.add_column("visura_properties", sa.Column("property_type", sa.String(), nullable=True))
        if _column_exists("visura_properties", "cadastre_type"):
            conn.execute(sa.text("""
                UPDATE visura_properties SET property_type =
                    CASE cadastre_type
                        WHEN 'F' THEN 'building'
                        WHEN 'T' THEN 'land'
                        WHEN 'E' THEN 'entity'
                        ELSE NULL
                    END
                WHERE property_type IS NULL
            """))

    if not _column_exists("visura_properties", "location_id"):
        op.add_column("visura_properties", sa.Column("location_id", sa.Integer(), nullable=True))
        op.create_index("ix_visura_properties_location_id", "visura_properties", ["location_id"])
        if _column_exists("visura_properties", "sheet"):
            # Derive cadastre_type from property_type or responses join for location population
            conn.execute(sa.text("""
                INSERT OR IGNORE INTO cadastral_locations
                    (cadastre_type, province, municipality, sheet, parcel, subunit, section)
                SELECT DISTINCT
                    COALESCE(
                        CASE property_type
                            WHEN 'building' THEN 'F'
                            WHEN 'land'     THEN 'T'
                            WHEN 'entity'   THEN 'E'
                            ELSE COALESCE(cadastre_type, '')
                        END, ''),
                    COALESCE(province, ''),
                    COALESCE(municipality, ''),
                    COALESCE(sheet, ''),
                    COALESCE(parcel, ''),
                    COALESCE(subunit, ''),
                    ''
                FROM visura_properties
                WHERE sheet IS NOT NULL AND sheet != ''
            """))
            conn.execute(sa.text("""
                UPDATE visura_properties SET location_id = (
                    SELECT id FROM cadastral_locations
                    WHERE cadastre_type = COALESCE(
                        CASE visura_properties.property_type
                            WHEN 'building' THEN 'F'
                            WHEN 'land'     THEN 'T'
                            WHEN 'entity'   THEN 'E'
                            ELSE COALESCE(visura_properties.cadastre_type, '')
                        END, '')
                      AND province     = COALESCE(visura_properties.province, '')
                      AND municipality = COALESCE(visura_properties.municipality, '')
                      AND sheet        = COALESCE(visura_properties.sheet, '')
                      AND parcel       = COALESCE(visura_properties.parcel, '')
                      AND subunit      = COALESCE(visura_properties.subunit, '')
                      AND section      = ''
                )
                WHERE location_id IS NULL AND sheet IS NOT NULL AND sheet != ''
            """))


def downgrade() -> None:
    conn = op.get_bind()

    if _column_exists("visura_properties", "location_id"):
        with op.batch_alter_table("visura_properties") as batch_op:
            batch_op.drop_index("ix_visura_properties_location_id")
            batch_op.drop_column("location_id")

    if _column_exists("visura_properties", "property_type"):
        with op.batch_alter_table("visura_properties") as batch_op:
            batch_op.drop_column("property_type")

    if _column_exists("visura_requests", "location_id"):
        with op.batch_alter_table("visura_requests") as batch_op:
            batch_op.drop_index("ix_visura_requests_location_id")
            batch_op.drop_column("location_id")

    if _column_exists("visura_documents", "requested_at"):
        with op.batch_alter_table("visura_documents") as batch_op:
            batch_op.drop_column("requested_at")

    if _column_exists("visura_documents", "subject"):
        with op.batch_alter_table("visura_documents") as batch_op:
            batch_op.drop_column("subject")
