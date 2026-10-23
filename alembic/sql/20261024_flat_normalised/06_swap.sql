-- Step 6: swap. The legacy tables and views move to `sister_legacy` (kept, untouched, for rollback and comparison);
-- the new tables and views move into `sister`. Relations this migration does not replace (PostGIS, alembic_version, job
-- queues, anything added by other migrations) stay where they are.
-- Moving a relation between schemas is a catalog change: it keeps data, indexes, sequences and view dependencies.
CREATE SCHEMA sister_legacy;

DO $$
DECLARE
    r record;
    -- Exactly the relations this migration replaces. Anything else in `sister` (job queues, feedback tables, tables added
    -- by other migrations, PostGIS, alembic_version) stays where it is.
    replaced text[] := ARRAY[
        'activity_log', 'building_addresses', 'building_classifications', 'building_current_states', 'building_identifiers', 'building_surfaces',
        'building_units', 'cadastral_locations', 'cadastral_map_geometry_cache', 'cadastral_subjects', 'document_metadata',
        'document_structured_extractions', 'document_subjects', 'document_xml_attributes', 'document_xml_nodes', 'enum_activity_action',
        'enum_activity_source', 'enum_activity_status', 'enum_building_category', 'enum_building_class', 'enum_building_consistency_unit',
        'enum_cadastral_subject_type', 'enum_cadastre_type', 'enum_gender', 'enum_land_class', 'enum_land_quality', 'enum_mortgage_lien_type',
        'enum_mortgage_note_type', 'enum_mortgage_party_quality', 'enum_mortgage_party_raw_quality', 'enum_mortgage_party_role', 'enum_mortgage_title_type',
        'enum_ownership_property_status', 'enum_page_visit_element_type', 'enum_visura_document_type', 'enum_visura_file_format',
        'enum_visura_property_category', 'enum_visura_property_class', 'enum_visura_property_quality', 'enum_visura_property_type',
        'enum_visura_request_type', 'geographic_places', 'land_classifications', 'land_parcels', 'mortgage_inspection_liens', 'mortgage_inspection_parties',
        'mortgage_inspection_properties', 'mortgage_inspection_titles', 'mortgage_inspection_units', 'mortgage_inspections', 'ownership_mutations',
        'ownership_right_catalog', 'ownership_rights', 'page_visit_errors', 'page_visit_form_elements', 'page_visits', 'properties_by_cadastral_parcel',
        'property_groups', 'property_owners', 'related_parcels', 'sister_owner_index', 'sister_property_index', 'v_owners_by_fiscal_code',
        'v_properties_by_cadastral_parcel', 'v_sister_cadastral_map_index', 'v_sister_document_by_cadastral_parcel', 'v_sister_document_context',
        'v_sister_owner_by_fiscal_code', 'v_sister_owner_context', 'v_sister_owner_property_links', 'v_sister_property_by_cadastral_parcel',
        'v_sister_property_by_fiscal_code', 'v_sister_request_context', 'v_sister_visura_cadastral_polygons', 'visura_documents', 'visura_owners',
        'visura_properties', 'visura_requests', 'visura_responses', 'visura_results'
    ];
BEGIN
    FOR r IN
        SELECT c.relname, c.relkind
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'sister' AND c.relkind IN ('r', 'p', 'v', 'm') AND c.relname = ANY (replaced)
        ORDER BY c.relkind DESC, c.relname   -- views before tables, for readability only
    LOOP
        EXECUTE format('ALTER %s sister.%I SET SCHEMA sister_legacy',
                       CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END, r.relname);
    END LOOP;
    -- the legacy enum-catalog triggers call this function; it refers to tables that now live in sister_legacy
    IF to_regprocedure('sister.ensure_enum_reference_value()') IS NOT NULL THEN
        ALTER FUNCTION sister.ensure_enum_reference_value() SET SCHEMA sister_legacy;
    END IF;

    FOR r IN
        SELECT c.relname, c.relkind
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'sister_next' AND c.relkind IN ('r', 'p', 'v', 'm')
        ORDER BY c.relkind DESC, c.relname
    LOOP
        EXECUTE format('ALTER %s sister_next.%I SET SCHEMA sister',
                       CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END, r.relname);
    END LOOP;
END $$;
DROP SCHEMA sister_next;

COMMENT ON SCHEMA sister_legacy IS 'Pre-20261024 layout, untouched by the flat/normalised migration. Safe to drop once nothing reads it.';
COMMENT ON SCHEMA sister_archive IS 'Raw payloads (response JSON, XML node tree, extraction JSON, document content) moved out of the working schema.';

-- sanity: the swap must have produced the new layout and parked the old one
DO $$
BEGIN
    IF to_regclass('sister.request') IS NULL OR to_regclass('sister.subject') IS NULL OR to_regclass('sister.v_property') IS NULL THEN
        RAISE EXCEPTION 'swap failed: new relations are not in schema sister';
    END IF;
    IF to_regclass('sister.visura_requests') IS NOT NULL OR to_regclass('sister_legacy.visura_requests') IS NULL THEN
        RAISE EXCEPTION 'swap failed: legacy relations were not parked in sister_legacy';
    END IF;
    IF to_regclass('sister.spatial_ref_sys') IS NULL OR to_regclass('sister.alembic_version') IS NULL THEN
        RAISE EXCEPTION 'swap failed: PostGIS / alembic_version must stay in schema sister';
    END IF;
END $$;
