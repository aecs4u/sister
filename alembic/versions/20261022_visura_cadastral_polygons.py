"""Cache cadastral polygons in Sister and expose polygons for visura documents.

The cadastral source lives in a separate PostgreSQL database, so a normal view
cannot join it directly. The application fills this small cache from that
source; this view attaches the cached PostGIS geometries to the normalized
parcel rows belonging to each visura document.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20261022_visura_polygons"
down_revision: Union[str, None] = "20261010_activity_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


_VIEW = "v_sister_visura_cadastral_polygons"
_CACHE = "cadastral_map_geometry_cache"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This SISTER schema migration requires PostgreSQL")

    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute(
        f"""CREATE TABLE IF NOT EXISTS {_CACHE} (
            feature_type text NOT NULL CHECK (feature_type IN ('parcel', 'sheet')),
            lookup_key text NOT NULL,
            municipality text NOT NULL DEFAULT '',
            province text NOT NULL DEFAULT '',
            municipality_code text NOT NULL DEFAULT '',
            section text NOT NULL DEFAULT '',
            sheet text NOT NULL DEFAULT '',
            parcel text NOT NULL DEFAULT '',
            source_reference text,
            geom geometry(Geometry, 4326),
            checked_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (feature_type, lookup_key)
        )"""
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_CACHE}_geom ON {_CACHE} USING GIST (geom)"
    )
    op.execute(
        f"""CREATE INDEX IF NOT EXISTS ix_{_CACHE}_parcel_lookup
            ON {_CACHE} (municipality_code, province, sheet, parcel)
            WHERE feature_type = 'parcel'"""
    )

    op.execute(f"DROP VIEW IF EXISTS {_VIEW}")
    op.execute(
        f"""CREATE VIEW {_VIEW} AS
        WITH document_locations AS (
            SELECT d.id AS document_id,
                   loc.id AS location_id,
                   COALESCE(NULLIF(btrim(m.municipality_code), ''), '') AS municipality_code,
                   COALESCE(NULLIF(btrim(loc.municipality), ''), '') AS municipality,
                   COALESCE(NULLIF(btrim(loc.province), ''), '') AS province,
                   COALESCE(NULLIF(btrim(loc.section), ''), '') AS section,
                   COALESCE(NULLIF(btrim(loc.sheet), ''), '') AS sheet,
                   COALESCE(NULLIF(btrim(loc.parcel), ''), '') AS parcel
            FROM visura_documents AS d
            JOIN document_metadata AS m ON m.id = d.id
            JOIN cadastral_locations AS loc ON loc.id = m.location_id
            WHERE d.document_type LIKE 'visura%'

            UNION ALL

            SELECT d.id,
                   loc.id,
                   COALESCE(NULLIF(btrim(p.municipality_code), ''), ''),
                   COALESCE(NULLIF(btrim(loc.municipality), ''), ''),
                   COALESCE(NULLIF(btrim(loc.province), ''), ''),
                   COALESCE(NULLIF(btrim(loc.section), ''), ''),
                   COALESCE(NULLIF(btrim(loc.sheet), ''), ''),
                   COALESCE(NULLIF(btrim(loc.parcel), ''), '')
            FROM land_parcels AS p
            JOIN visura_documents AS d ON d.id = p.document_id
            JOIN cadastral_locations AS loc ON loc.id = p.location_id
            WHERE d.document_type LIKE 'visura%'

            UNION ALL

            SELECT d.id,
                   loc.id,
                   COALESCE(NULLIF(btrim(bi.municipality_code), ''),
                            NULLIF(btrim(pg.municipality_code), ''), ''),
                   COALESCE(NULLIF(btrim(loc.municipality), ''), ''),
                   COALESCE(NULLIF(btrim(loc.province), ''), ''),
                   COALESCE(NULLIF(btrim(loc.section), ''), ''),
                   COALESCE(NULLIF(btrim(loc.sheet), ''), ''),
                   COALESCE(NULLIF(btrim(loc.parcel), ''), '')
            FROM property_groups AS pg
            JOIN visura_documents AS d ON d.id = pg.document_id
            JOIN building_units AS bu ON bu.group_id = pg.id
            JOIN building_identifiers AS bi ON bi.building_unit_id = bu.id
            JOIN cadastral_locations AS loc ON loc.id = bi.location_id
            WHERE d.document_type LIKE 'visura%'

            UNION ALL

            SELECT d.id,
                   loc.id,
                   COALESCE(NULLIF(btrim(bi.municipality_code), ''), ''),
                   COALESCE(NULLIF(btrim(loc.municipality), ''), ''),
                   COALESCE(NULLIF(btrim(loc.province), ''), ''),
                   COALESCE(NULLIF(btrim(loc.section), ''), ''),
                   COALESCE(NULLIF(btrim(loc.sheet), ''), ''),
                   COALESCE(NULLIF(btrim(loc.parcel), ''), '')
            FROM building_current_states AS state
            JOIN visura_documents AS d ON d.id = state.id
            JOIN building_identifiers AS bi ON bi.current_state_id = state.id
            JOIN cadastral_locations AS loc ON loc.id = bi.location_id
            WHERE d.document_type LIKE 'visura%'
        ), valid_locations AS (
            SELECT DISTINCT document_id, location_id, municipality_code,
                            municipality, province, section, sheet, parcel
            FROM document_locations
            WHERE municipality <> '' AND province <> '' AND sheet <> '' AND parcel <> ''
        ), document_features AS (
            SELECT DISTINCT document_id,
                   'parcel'::text AS feature_type,
                   concat_ws(chr(31), upper(btrim(municipality)), upper(btrim(province)),
                             upper(btrim(municipality_code)), upper(btrim(section)),
                             upper(btrim(sheet)), upper(btrim(parcel))) AS lookup_key,
                   location_id, municipality_code, municipality, province,
                   section, sheet, parcel
            FROM valid_locations

            UNION

            SELECT DISTINCT document_id,
                   'sheet'::text AS feature_type,
                   concat_ws(chr(31), upper(btrim(municipality)), upper(btrim(province)),
                             upper(btrim(municipality_code)), upper(btrim(section)),
                             upper(btrim(sheet))) AS lookup_key,
                   location_id, municipality_code, municipality, province,
                   section, sheet, ''::text AS parcel
            FROM valid_locations
        ), unique_features AS (
            SELECT DISTINCT ON (document_id, feature_type, lookup_key)
                   document_id, feature_type, lookup_key, location_id,
                   municipality_code, municipality, province, section, sheet, parcel
            FROM document_features
            ORDER BY document_id, feature_type, lookup_key, location_id
        )
        SELECT f.document_id,
               f.feature_type,
               f.lookup_key,
               f.location_id,
               f.municipality_code,
               f.municipality,
               f.province,
               f.section,
               f.sheet,
               f.parcel,
               cache.source_reference,
               cache.checked_at,
               cache.geom
        FROM unique_features AS f
        LEFT JOIN {_CACHE} AS cache
          ON cache.feature_type = f.feature_type
         AND cache.lookup_key = f.lookup_key"""
    )
    op.execute(
        f"COMMENT ON VIEW {_VIEW} IS 'Visura document sheet and parcel references with cached cadastral PostGIS polygons'"
    )


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {_VIEW}")
    op.execute(f"DROP TABLE IF EXISTS {_CACHE}")
