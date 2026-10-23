-- Step 1: deduplicate records before the redesign. Every removed row is logged in dedup_audit.removed.
-- Idempotent: a second run finds nothing to merge. Runs on the legacy layout, inside the migration transaction.
SET LOCAL search_path = sister, public;

CREATE SCHEMA IF NOT EXISTS dedup_audit;
CREATE TABLE IF NOT EXISTS dedup_audit.removed (
    run_at timestamptz NOT NULL DEFAULT now(),
    table_name text NOT NULL,
    removed_id bigint NOT NULL,
    kept_id bigint,
    reason text NOT NULL
);

-- ---------------------------------------------------------------------------
-- 1. Documents imported twice under both path aliases (/data -> /mnt/mobile/data symlink)
--    Keep the oldest row, delete the later one with its whole row tree.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE doc_dupes ON COMMIT DROP AS
SELECT id AS removed_id, kept_id FROM (
    SELECT id, first_value(id) OVER (PARTITION BY regexp_replace(file_path, '^(/mnt/mobile)?/data/', '')
                                     ORDER BY created_at, id) AS kept_id
    FROM visura_documents
) x WHERE id <> kept_id;

INSERT INTO dedup_audit.removed (table_name, removed_id, kept_id, reason)
SELECT 'visura_documents', removed_id, kept_id, 'same file under /data and /mnt/mobile/data' FROM doc_dupes;

CREATE TEMP TABLE d_groups ON COMMIT DROP AS SELECT id FROM property_groups WHERE document_id IN (SELECT removed_id FROM doc_dupes);
CREATE TEMP TABLE d_units ON COMMIT DROP AS SELECT id FROM building_units WHERE group_id IN (SELECT id FROM d_groups);
CREATE TEMP TABLE d_parcels ON COMMIT DROP AS SELECT id FROM land_parcels WHERE document_id IN (SELECT removed_id FROM doc_dupes);
CREATE TEMP TABLE d_mutations ON COMMIT DROP AS
SELECT id FROM ownership_mutations
WHERE document_id IN (SELECT removed_id FROM doc_dupes)
   OR land_parcel_id IN (SELECT id FROM d_parcels)
   OR property_group_id IN (SELECT id FROM d_groups);
CREATE TEMP TABLE d_extractions ON COMMIT DROP AS
SELECT id FROM document_structured_extractions WHERE document_id IN (SELECT removed_id FROM doc_dupes);

DELETE FROM property_owners WHERE mutation_id IN (SELECT id FROM d_mutations);
DELETE FROM ownership_mutations WHERE id IN (SELECT id FROM d_mutations);
DELETE FROM land_classifications WHERE parcel_id IN (SELECT id FROM d_parcels);
DELETE FROM land_parcels WHERE id IN (SELECT id FROM d_parcels);
DELETE FROM related_parcels WHERE building_unit_id IN (SELECT id FROM d_units)
    OR current_state_id IN (SELECT removed_id FROM doc_dupes);
DELETE FROM building_classifications WHERE building_unit_id IN (SELECT id FROM d_units)
    OR current_state_id IN (SELECT removed_id FROM doc_dupes)
    OR history_document_id IN (SELECT removed_id FROM doc_dupes);
DELETE FROM building_identifiers WHERE building_unit_id IN (SELECT id FROM d_units)
    OR current_state_id IN (SELECT removed_id FROM doc_dupes)
    OR history_document_id IN (SELECT removed_id FROM doc_dupes);
DELETE FROM building_surfaces WHERE building_unit_id IN (SELECT id FROM d_units)
    OR current_state_id IN (SELECT removed_id FROM doc_dupes);
DELETE FROM building_units WHERE id IN (SELECT id FROM d_units);
DELETE FROM property_groups WHERE id IN (SELECT id FROM d_groups);
DELETE FROM building_addresses WHERE document_id IN (SELECT removed_id FROM doc_dupes);
DELETE FROM building_current_states WHERE id IN (SELECT removed_id FROM doc_dupes);
DELETE FROM document_xml_attributes WHERE node_id IN
    (SELECT id FROM document_xml_nodes WHERE document_id IN (SELECT removed_id FROM doc_dupes));
DELETE FROM document_xml_nodes WHERE document_id IN (SELECT removed_id FROM doc_dupes);
DELETE FROM document_metadata WHERE id IN (SELECT removed_id FROM doc_dupes);
DELETE FROM document_subjects WHERE id IN (SELECT removed_id FROM doc_dupes);
DELETE FROM mortgage_inspection_properties WHERE unit_id IN
    (SELECT id FROM mortgage_inspection_units WHERE extraction_id IN (SELECT id FROM d_extractions));
DELETE FROM mortgage_inspection_units WHERE extraction_id IN (SELECT id FROM d_extractions);
DELETE FROM mortgage_inspection_parties WHERE extraction_id IN (SELECT id FROM d_extractions);
DELETE FROM mortgage_inspection_liens WHERE extraction_id IN (SELECT id FROM d_extractions);
DELETE FROM mortgage_inspection_titles WHERE extraction_id IN (SELECT id FROM d_extractions);
DELETE FROM mortgage_inspections WHERE extraction_id IN (SELECT id FROM d_extractions);
DELETE FROM document_structured_extractions WHERE id IN (SELECT id FROM d_extractions);
DELETE FROM visura_documents WHERE id IN (SELECT removed_id FROM doc_dupes);

-- ---------------------------------------------------------------------------
-- 2. cadastral_locations: trailing/leading whitespace and '' vs NULL made one place several rows
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE loc_map ON COMMIT DROP AS
SELECT id AS removed_id, kept_id FROM (
    SELECT id, min(id) OVER (PARTITION BY cadastre_type, upper(NULLIF(btrim(province), '')), upper(NULLIF(btrim(municipality), '')),
                             NULLIF(btrim(sheet), ''), NULLIF(btrim(parcel), ''), NULLIF(btrim(subunit), ''),
                             upper(NULLIF(btrim(section), ''))) AS kept_id
    FROM cadastral_locations
) x WHERE id <> kept_id;

INSERT INTO dedup_audit.removed (table_name, removed_id, kept_id, reason)
SELECT 'cadastral_locations', removed_id, kept_id, 'same place after trimming whitespace / folding case' FROM loc_map;

UPDATE visura_properties t SET location_id = m.kept_id FROM loc_map m WHERE t.location_id = m.removed_id;
UPDATE visura_requests t SET location_id = m.kept_id FROM loc_map m WHERE t.location_id = m.removed_id;
UPDATE building_identifiers t SET location_id = m.kept_id FROM loc_map m WHERE t.location_id = m.removed_id;
UPDATE land_parcels t SET location_id = m.kept_id FROM loc_map m WHERE t.location_id = m.removed_id;
UPDATE related_parcels t SET location_id = m.kept_id FROM loc_map m WHERE t.location_id = m.removed_id;
UPDATE ownership_mutations t SET reference_location_id = m.kept_id FROM loc_map m WHERE t.reference_location_id = m.removed_id;
UPDATE document_metadata t SET location_id = m.kept_id FROM loc_map m WHERE t.location_id = m.removed_id;
DELETE FROM cadastral_locations WHERE id IN (SELECT removed_id FROM loc_map);

-- survivors: store the trimmed form (no collisions left now)
UPDATE cadastral_locations SET
    province = upper(NULLIF(btrim(province), '')),
    municipality = upper(NULLIF(btrim(municipality), '')),
    sheet = NULLIF(btrim(sheet), ''),
    parcel = NULLIF(btrim(parcel), ''),
    subunit = NULLIF(btrim(subunit), ''),
    section = upper(NULLIF(btrim(section), ''))
WHERE province IS DISTINCT FROM upper(NULLIF(btrim(province), ''))
   OR municipality IS DISTINCT FROM upper(NULLIF(btrim(municipality), ''))
   OR sheet IS DISTINCT FROM NULLIF(btrim(sheet), '')
   OR parcel IS DISTINCT FROM NULLIF(btrim(parcel), '')
   OR subunit IS DISTINCT FROM NULLIF(btrim(subunit), '')
   OR section IS DISTINCT FROM upper(NULLIF(btrim(section), ''));

-- ---------------------------------------------------------------------------
-- 3. cadastral_subjects without a fiscal code: identical identity, or the same name + birth date as a
--    subject that has one (keep the row with the fiscal code, else the oldest)
-- ---------------------------------------------------------------------------
UPDATE cadastral_subjects SET fiscal_code = upper(btrim(fiscal_code))
WHERE fiscal_code IS DISTINCT FROM upper(btrim(fiscal_code));

CREATE TEMP TABLE subj_map ON COMMIT DROP AS
WITH ident AS (
    SELECT id, fiscal_code,
           upper(btrim(coalesce(display_name, ''))) AS nm, date_of_birth AS dob,
           birth_place_id, coalesce(subject_type, '') AS st
    FROM cadastral_subjects
), no_cf_groups AS (
    SELECT id, first_value(id) OVER (PARTITION BY nm, dob, birth_place_id, st ORDER BY id) AS kept_id
    FROM ident WHERE fiscal_code IS NULL
), by_name_dob AS (
    SELECT n.id, min(c.id) AS kept_id
    FROM ident n JOIN ident c ON c.fiscal_code IS NOT NULL AND c.nm = n.nm AND c.dob = n.dob
    WHERE n.fiscal_code IS NULL AND n.dob IS NOT NULL AND n.nm <> ''
    GROUP BY n.id
)
SELECT g.id AS removed_id, coalesce(b.kept_id, g.kept_id) AS kept_id
FROM no_cf_groups g LEFT JOIN by_name_dob b ON b.id = g.kept_id  -- resolve via the group's representative: no chains
WHERE g.id <> coalesce(b.kept_id, g.kept_id);

INSERT INTO dedup_audit.removed (table_name, removed_id, kept_id, reason)
SELECT 'cadastral_subjects', removed_id, kept_id, 'same identity (no fiscal code)' FROM subj_map;

UPDATE document_subjects t SET subject_id = m.kept_id FROM subj_map m WHERE t.subject_id = m.removed_id;
UPDATE property_owners t SET subject_id = m.kept_id FROM subj_map m WHERE t.subject_id = m.removed_id;
UPDATE visura_owners t SET subject_id = m.kept_id FROM subj_map m WHERE t.subject_id = m.removed_id;
UPDATE visura_properties t SET subject_id = m.kept_id FROM subj_map m WHERE t.subject_id = m.removed_id;
DELETE FROM cadastral_subjects WHERE id IN (SELECT removed_id FROM subj_map);

-- ---------------------------------------------------------------------------
-- 4. Link rows that now repeat: the same owner + right twice in one mutation / one response
-- ---------------------------------------------------------------------------
WITH ranked AS (
    SELECT id, first_value(id) OVER (PARTITION BY mutation_id, subject_id, right_id ORDER BY id) AS kept_id
    FROM property_owners
), gone AS (
    DELETE FROM property_owners p USING ranked r
    WHERE p.id = r.id AND r.id <> r.kept_id RETURNING p.id, r.kept_id
)
INSERT INTO dedup_audit.removed (table_name, removed_id, kept_id, reason)
SELECT 'property_owners', id, kept_id, 'same subject and right twice in one mutation' FROM gone;

WITH ranked AS (
    SELECT id, first_value(id) OVER (PARTITION BY response_id, result_id, subject_id, right_id ORDER BY id) AS kept_id
    FROM visura_owners
), gone AS (
    DELETE FROM visura_owners p USING ranked r
    WHERE p.id = r.id AND r.id <> r.kept_id RETURNING p.id, r.kept_id
)
INSERT INTO dedup_audit.removed (table_name, removed_id, kept_id, reason)
SELECT 'visura_owners', id, kept_id, 'same subject and right twice in one response' FROM gone;
