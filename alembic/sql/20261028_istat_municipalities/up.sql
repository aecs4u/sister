-- Places are the shared ISTAT municipality table (`istat.municipalities`, a postgres_fdw foreign table), not a table of our
-- own. Every `*_comune_id` column now holds ISTAT's `numeric_code`; PostgreSQL cannot reference a foreign table, so these
-- columns have no foreign key and the views (views.sql) join the table explicitly.
--
--   * cadastral_unit.comune_id, property.registered_office_comune_id, property_group.comune_id,
--     parcel_geometry_cache.comune_id, subject.birth_comune_id: comune.id -> numeric_code (matched by Belfiore code,
--     else by a unique name + province)
--   * a birth place ISTAT does not know (a foreign country, a comune merged away) stays as text on the subject:
--     subject.birth_municipality_text + birth_province_code
--   * the `comune` table is kept as `comune_pre_istat`, the snapshot down.sql restores from
-- `__SISTER_SCHEMA__` / `__ISTAT_SCHEMA__` are replaced by the (quoted) schema names when the file is run.

DO $$
BEGIN
    IF to_regclass('__ISTAT_SCHEMA__.municipalities') IS NULL THEN
        RAISE EXCEPTION 'istat.municipalities is required: the shared ISTAT municipality table is the reference for places';
    END IF;
END $$;

-- the views that mention places are rebuilt by views.sql
DROP VIEW IF EXISTS __SISTER_SCHEMA__.v_visura_cadastral_polygon;
DROP VIEW IF EXISTS __SISTER_SCHEMA__.v_cadastral_unit_index;
DROP VIEW IF EXISTS __SISTER_SCHEMA__.v_document_cadastral_unit;
DROP VIEW IF EXISTS __SISTER_SCHEMA__.v_property_owner;
DROP VIEW IF EXISTS __SISTER_SCHEMA__.v_result_owner;
DROP VIEW IF EXISTS __SISTER_SCHEMA__.v_property;
DROP VIEW IF EXISTS __SISTER_SCHEMA__.v_cadastral_unit;

ALTER TABLE __SISTER_SCHEMA__.subject ADD COLUMN IF NOT EXISTS birth_municipality_text text;
ALTER TABLE __SISTER_SCHEMA__.subject ADD COLUMN IF NOT EXISTS birth_province_code char(2);

DO $$
DECLARE
    r record;
    k record;
    swapped integer := 0;
BEGIN
    IF to_regclass('__SISTER_SCHEMA__.comune') IS NULL THEN
        RAISE NOTICE 'comune already converted: nothing to do';
        RETURN;
    END IF;

    -- comune.id -> ISTAT numeric_code
    CREATE TEMP TABLE _comune_map ON COMMIT DROP AS
    SELECT c.id AS comune_id, c.name, c.province_code,
           coalesce(
               (SELECT min(i.numeric_code)::integer FROM __ISTAT_SCHEMA__.municipalities i
                WHERE c.cadastral_code IS NOT NULL AND upper(btrim(i.cadastral_code)) = upper(c.cadastral_code)),
               (SELECT min(i.numeric_code)::integer FROM __ISTAT_SCHEMA__.municipalities i
                WHERE upper(btrim(i.province_abbreviation)) = c.province_code
                  AND c.name IN (upper(btrim(i.name)), upper(btrim(i.italian_name)))
                HAVING count(DISTINCT i.numeric_code) = 1)
           ) AS numeric_code
    FROM __SISTER_SCHEMA__.comune c;

    -- places ISTAT does not know survive as text on the subject
    UPDATE __SISTER_SCHEMA__.subject s
    SET birth_municipality_text = m.name, birth_province_code = m.province_code
    FROM _comune_map m
    WHERE m.comune_id = s.birth_comune_id AND m.numeric_code IS NULL;

    -- swap every column through a new one, so an old id and a new code can never be mistaken for each other
    FOR r IN SELECT * FROM (VALUES
        ('cadastral_unit', 'comune_id'), ('property', 'registered_office_comune_id'), ('property_group', 'comune_id'),
        ('parcel_geometry_cache', 'comune_id'), ('subject', 'birth_comune_id')
    ) AS t (tbl, col)
    LOOP
        EXECUTE format('ALTER TABLE __SISTER_SCHEMA__.%I ADD COLUMN _istat integer', r.tbl);
        EXECUTE format('UPDATE __SISTER_SCHEMA__.%I t SET _istat = m.numeric_code FROM _comune_map m WHERE m.comune_id = t.%I',
                       r.tbl, r.col);
        FOR k IN
            SELECT conname FROM pg_constraint
            WHERE conrelid = format('__SISTER_SCHEMA__.%I', r.tbl)::regclass AND contype IN ('f', 'u', 'p')
              AND (SELECT attnum FROM pg_attribute
                   WHERE attrelid = format('__SISTER_SCHEMA__.%I', r.tbl)::regclass AND attname = r.col) = ANY (conkey)
        LOOP
            EXECUTE format('ALTER TABLE __SISTER_SCHEMA__.%I DROP CONSTRAINT %I', r.tbl, k.conname);
        END LOOP;
        EXECUTE format('ALTER TABLE __SISTER_SCHEMA__.%I DROP COLUMN %I CASCADE', r.tbl, r.col);
        EXECUTE format('ALTER TABLE __SISTER_SCHEMA__.%I RENAME COLUMN _istat TO %I', r.tbl, r.col);
        swapped := swapped + 1;
    END LOOP;

    -- what the dropped columns carried
    ALTER TABLE __SISTER_SCHEMA__.cadastral_unit ADD CONSTRAINT uq_cadastral_unit
        UNIQUE NULLS NOT DISTINCT (cadastre_type, comune_id, section, sheet, parcel, subunit);
    CREATE INDEX IF NOT EXISTS ix_cadastral_unit_comune ON __SISTER_SCHEMA__.cadastral_unit (comune_id);
    CREATE INDEX IF NOT EXISTS ix_property_registered_office ON __SISTER_SCHEMA__.property (registered_office_comune_id);
    CREATE INDEX IF NOT EXISTS ix_property_group_comune ON __SISTER_SCHEMA__.property_group (comune_id);
    CREATE INDEX IF NOT EXISTS ix_subject_birth_comune ON __SISTER_SCHEMA__.subject (birth_comune_id);
    DELETE FROM __SISTER_SCHEMA__.parcel_geometry_cache WHERE comune_id IS NULL;  -- a polygon of a place ISTAT lacks is of no use
    ALTER TABLE __SISTER_SCHEMA__.parcel_geometry_cache ALTER COLUMN comune_id SET NOT NULL;
    ALTER TABLE __SISTER_SCHEMA__.parcel_geometry_cache ADD PRIMARY KEY (feature_type, comune_id, section, sheet, parcel);
    CREATE INDEX IF NOT EXISTS ix_parcel_geometry_cache_comune ON __SISTER_SCHEMA__.parcel_geometry_cache (comune_id);
    ALTER TABLE __SISTER_SCHEMA__.subject ADD CONSTRAINT ck_subject_birth_province CHECK (birth_province_code ~ '^[A-Z]{2}$');

    ALTER TABLE __SISTER_SCHEMA__.comune RENAME TO comune_pre_istat;  -- the snapshot down.sql restores from
    RAISE NOTICE '% columns now hold ISTAT numeric codes', swapped;
END $$;
