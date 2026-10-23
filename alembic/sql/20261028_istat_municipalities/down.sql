-- Restore the prior local reference table and translate the current ISTAT keys back to its ids.
DROP VIEW IF EXISTS __SISTER_SCHEMA__.v_visura_cadastral_polygon;
DROP VIEW IF EXISTS __SISTER_SCHEMA__.v_cadastral_unit_index;
DROP VIEW IF EXISTS __SISTER_SCHEMA__.v_document_cadastral_unit;
DROP VIEW IF EXISTS __SISTER_SCHEMA__.v_property_owner;
DROP VIEW IF EXISTS __SISTER_SCHEMA__.v_result_owner;
DROP VIEW IF EXISTS __SISTER_SCHEMA__.v_property;
DROP VIEW IF EXISTS __SISTER_SCHEMA__.v_cadastral_unit;

DO $$
BEGIN
    IF to_regclass('__SISTER_SCHEMA__.comune_pre_istat') IS NULL THEN
        RAISE EXCEPTION 'Rollback snapshot __SISTER_SCHEMA__.comune_pre_istat is missing';
    END IF;
END $$;

ALTER TABLE __SISTER_SCHEMA__.comune_pre_istat RENAME TO comune;

-- Recreate missing current Italian places for references created after the upgrade.
WITH used_codes AS (
    SELECT comune_id::integer AS numeric_code FROM __SISTER_SCHEMA__.cadastral_unit WHERE comune_id IS NOT NULL
    UNION SELECT birth_comune_id::integer FROM __SISTER_SCHEMA__.subject WHERE birth_comune_id IS NOT NULL
    UNION SELECT registered_office_comune_id::integer FROM __SISTER_SCHEMA__.property WHERE registered_office_comune_id IS NOT NULL
    UNION SELECT comune_id::integer FROM __SISTER_SCHEMA__.property_group WHERE comune_id IS NOT NULL
    UNION SELECT comune_id::integer FROM __SISTER_SCHEMA__.parcel_geometry_cache
)
INSERT INTO __SISTER_SCHEMA__.comune (name, province_code, cadastral_code)
SELECT upper(coalesce(m.italian_name, m.name)), m.province_abbreviation, nullif(btrim(m.cadastral_code), '')
FROM used_codes u JOIN __ISTAT_SCHEMA__.municipalities m ON m.numeric_code = u.numeric_code
WHERE NOT EXISTS (
    SELECT 1 FROM __SISTER_SCHEMA__.comune c
    WHERE (nullif(btrim(m.cadastral_code), '') IS NOT NULL
           AND upper(btrim(c.cadastral_code)) = upper(btrim(m.cadastral_code)))
       OR (upper(btrim(c.name)) = upper(btrim(coalesce(m.italian_name, m.name)))
           AND c.province_code IS NOT DISTINCT FROM m.province_abbreviation)
);

-- Preserve foreign and historical birthplaces that were stored as text after the upgrade.
INSERT INTO __SISTER_SCHEMA__.comune (name, province_code)
SELECT upper(btrim(s.birth_municipality_text)), nullif(upper(btrim(s.birth_province_code)), '')
FROM __SISTER_SCHEMA__.subject s
WHERE nullif(btrim(s.birth_municipality_text), '') IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM __SISTER_SCHEMA__.comune c
      WHERE upper(btrim(c.name)) = upper(btrim(s.birth_municipality_text))
        AND c.province_code IS NOT DISTINCT FROM nullif(upper(btrim(s.birth_province_code)), '')
  )
ON CONFLICT ON CONSTRAINT uq_comune_province_name DO NOTHING;

CREATE TEMP TABLE _sister_istat_municipality_restore_map ON COMMIT DROP AS
SELECT m.numeric_code, c.id AS local_id
FROM __ISTAT_SCHEMA__.municipalities m
LEFT JOIN LATERAL (
    SELECT min(x.id) AS id
    FROM __SISTER_SCHEMA__.comune x
    WHERE (nullif(btrim(m.cadastral_code), '') IS NOT NULL
           AND upper(btrim(x.cadastral_code)) = upper(btrim(m.cadastral_code)))
       OR (upper(btrim(x.name)) = upper(btrim(coalesce(m.italian_name, m.name)))
           AND x.province_code IS NOT DISTINCT FROM m.province_abbreviation)
) c ON true;

UPDATE __SISTER_SCHEMA__.cadastral_unit x SET comune_id = m.local_id
FROM _sister_istat_municipality_restore_map m WHERE x.comune_id = m.numeric_code AND m.local_id IS NOT NULL;
UPDATE __SISTER_SCHEMA__.subject x SET birth_comune_id = m.local_id
FROM _sister_istat_municipality_restore_map m WHERE x.birth_comune_id = m.numeric_code AND m.local_id IS NOT NULL;
UPDATE __SISTER_SCHEMA__.property x SET registered_office_comune_id = m.local_id
FROM _sister_istat_municipality_restore_map m WHERE x.registered_office_comune_id = m.numeric_code AND m.local_id IS NOT NULL;
UPDATE __SISTER_SCHEMA__.property_group x SET comune_id = m.local_id
FROM _sister_istat_municipality_restore_map m WHERE x.comune_id = m.numeric_code AND m.local_id IS NOT NULL;
UPDATE __SISTER_SCHEMA__.parcel_geometry_cache x SET comune_id = m.local_id
FROM _sister_istat_municipality_restore_map m WHERE x.comune_id = m.numeric_code AND m.local_id IS NOT NULL;

UPDATE __SISTER_SCHEMA__.subject s SET birth_comune_id = c.id
FROM __SISTER_SCHEMA__.comune c
WHERE s.birth_comune_id IS NULL
  AND nullif(btrim(s.birth_municipality_text), '') IS NOT NULL
  AND upper(btrim(c.name)) = upper(btrim(s.birth_municipality_text))
  AND c.province_code IS NOT DISTINCT FROM nullif(upper(btrim(s.birth_province_code)), '');

ALTER TABLE __SISTER_SCHEMA__.cadastral_unit ALTER COLUMN comune_id TYPE bigint USING comune_id::bigint;
ALTER TABLE __SISTER_SCHEMA__.subject ALTER COLUMN birth_comune_id TYPE bigint USING birth_comune_id::bigint;
ALTER TABLE __SISTER_SCHEMA__.property ALTER COLUMN registered_office_comune_id TYPE bigint USING registered_office_comune_id::bigint;
ALTER TABLE __SISTER_SCHEMA__.property_group ALTER COLUMN comune_id TYPE bigint USING comune_id::bigint;
ALTER TABLE __SISTER_SCHEMA__.parcel_geometry_cache ALTER COLUMN comune_id TYPE bigint USING comune_id::bigint;

ALTER TABLE __SISTER_SCHEMA__.cadastral_unit
    ADD CONSTRAINT cadastral_unit_comune_id_fkey FOREIGN KEY (comune_id) REFERENCES __SISTER_SCHEMA__.comune (id);
ALTER TABLE __SISTER_SCHEMA__.subject
    ADD CONSTRAINT subject_birth_comune_id_fkey FOREIGN KEY (birth_comune_id) REFERENCES __SISTER_SCHEMA__.comune (id);
ALTER TABLE __SISTER_SCHEMA__.property
    ADD CONSTRAINT property_registered_office_comune_id_fkey FOREIGN KEY (registered_office_comune_id) REFERENCES __SISTER_SCHEMA__.comune (id);
ALTER TABLE __SISTER_SCHEMA__.property_group
    ADD CONSTRAINT property_group_comune_id_fkey FOREIGN KEY (comune_id) REFERENCES __SISTER_SCHEMA__.comune (id);
ALTER TABLE __SISTER_SCHEMA__.parcel_geometry_cache
    ADD CONSTRAINT parcel_geometry_cache_comune_id_fkey FOREIGN KEY (comune_id) REFERENCES __SISTER_SCHEMA__.comune (id);

ALTER TABLE __SISTER_SCHEMA__.subject DROP COLUMN birth_municipality_text;
ALTER TABLE __SISTER_SCHEMA__.subject DROP COLUMN birth_province_code;
