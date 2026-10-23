-- Downgrade: put the legacy layout back. Rows written to the new layout after the upgrade are discarded, and the
-- records removed by the dedup step are not restored (restore from a backup for that; ids are in dedup_audit.removed).
DO $$
BEGIN
    IF to_regclass('sister_legacy.visura_requests') IS NULL THEN
        RAISE EXCEPTION 'cannot downgrade: sister_legacy no longer holds the previous layout';
    END IF;
END $$;

CREATE SCHEMA sister_next;
DO $$
DECLARE
    r record;
    -- the relations the upgrade created in `sister`; anything else there is not ours to move
    created text[] := ARRAY[
        'activity_action', 'activity_log', 'activity_log_attribute', 'building_unit', 'building_unit_related_parcel', 'cadastral_category',
        'cadastral_unit', 'comune', 'document', 'document_address', 'document_extraction', 'document_subject', 'document_type', 'land_parcel',
        'land_parcel_classification', 'land_quality', 'land_registry_office', 'mortgage_inspection', 'mortgage_inspection_party',
        'mortgage_inspection_property', 'mortgage_lien_type', 'mortgage_note_type', 'mortgage_party_quality', 'mortgage_title_type',
        'mutation_owner', 'ownership_mutation', 'ownership_right', 'page_visit', 'page_visit_error', 'page_visit_form_element',
        'parcel_geometry_cache', 'property', 'property_group', 'request', 'request_result', 'request_type', 'result_owner', 'right_kind',
        'subject', 'v_cadastral_unit', 'v_cadastral_unit_index', 'v_document_cadastral_unit', 'v_property', 'v_property_owner', 'v_result_owner',
        'v_visura_cadastral_polygon'
    ];
BEGIN
    FOR r IN
        SELECT c.relname, c.relkind
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'sister' AND c.relkind IN ('r', 'p', 'v', 'm') AND c.relname = ANY (created)
        ORDER BY c.relkind DESC, c.relname
    LOOP
        EXECUTE format('ALTER %s sister.%I SET SCHEMA sister_next',
                       CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END, r.relname);
    END LOOP;

    FOR r IN
        SELECT c.relname, c.relkind
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'sister_legacy' AND c.relkind IN ('r', 'p', 'v', 'm')
        ORDER BY c.relkind DESC, c.relname
    LOOP
        EXECUTE format('ALTER %s sister_legacy.%I SET SCHEMA sister',
                       CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END, r.relname);
    END LOOP;
    IF to_regprocedure('sister_legacy.ensure_enum_reference_value()') IS NOT NULL THEN
        ALTER FUNCTION sister_legacy.ensure_enum_reference_value() SET SCHEMA sister;
    END IF;
END $$;

DROP SCHEMA sister_archive CASCADE;
DROP SCHEMA sister_next CASCADE;
DROP SCHEMA sister_legacy;
