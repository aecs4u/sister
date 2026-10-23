-- Step 4: reconcile sister_next against the deduplicated legacy tables. Any mismatch aborts the whole migration
-- (it runs in one transaction), so a bad load can never be swapped in.
SET LOCAL search_path = sister, public;

CREATE FUNCTION pg_temp.expect(label text, expected numeric, actual numeric) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF expected IS DISTINCT FROM actual THEN
        RAISE EXCEPTION 'migration check failed: % (expected %, got %)', label, expected, actual;
    END IF;
END $$;

-- ---- entities that map 1:1 keep their row count ----------------------------------------------------------
SELECT pg_temp.expect('request', (SELECT count(*) FROM visura_requests), (SELECT count(*) FROM sister_next.request));
SELECT pg_temp.expect('request_result', (SELECT count(*) FROM visura_results), (SELECT count(*) FROM sister_next.request_result));
SELECT pg_temp.expect('property', (SELECT count(*) FROM visura_properties), (SELECT count(*) FROM sister_next.property));
SELECT pg_temp.expect('document', (SELECT count(*) FROM visura_documents), (SELECT count(*) FROM sister_next.document));
SELECT pg_temp.expect('document_subject', (SELECT count(*) FROM document_subjects WHERE subject_id IS NOT NULL), (SELECT count(*) FROM sister_next.document_subject));
SELECT pg_temp.expect('document_address', (SELECT count(*) FROM building_addresses WHERE nullif(btrim(address_text), '') IS NOT NULL), (SELECT count(*) FROM sister_next.document_address));
SELECT pg_temp.expect('property_group', (SELECT count(*) FROM property_groups), (SELECT count(*) FROM sister_next.property_group));
SELECT pg_temp.expect('building_unit (units + current states)', (SELECT count(*) FROM building_units) + (SELECT count(*) FROM building_current_states), (SELECT count(*) FROM sister_next.building_unit));
SELECT pg_temp.expect('building_unit current', (SELECT count(*) FROM building_current_states), (SELECT count(*) FROM sister_next.building_unit WHERE is_current));
SELECT pg_temp.expect('land_parcel', (SELECT count(*) FROM land_parcels), (SELECT count(*) FROM sister_next.land_parcel));
SELECT pg_temp.expect('land_parcel_classification', (SELECT count(*) FROM land_classifications), (SELECT count(*) FROM sister_next.land_parcel_classification));
SELECT pg_temp.expect('ownership_mutation', (SELECT count(*) FROM ownership_mutations), (SELECT count(*) FROM sister_next.ownership_mutation));
SELECT pg_temp.expect('page_visit', (SELECT count(*) FROM page_visits), (SELECT count(*) FROM sister_next.page_visit));
SELECT pg_temp.expect('page_visit_form_element', (SELECT count(*) FROM page_visit_form_elements), (SELECT count(*) FROM sister_next.page_visit_form_element));
SELECT pg_temp.expect('page_visit_error', (SELECT count(*) FROM page_visit_errors), (SELECT count(*) FROM sister_next.page_visit_error));
SELECT pg_temp.expect('activity_log', (SELECT count(*) FROM activity_log), (SELECT count(*) FROM sister_next.activity_log));
SELECT pg_temp.expect('document_extraction', (SELECT count(*) FROM document_structured_extractions), (SELECT count(*) FROM sister_next.document_extraction));
SELECT pg_temp.expect('mortgage_inspection', (SELECT count(*) FROM mortgage_inspections), (SELECT count(*) FROM sister_next.mortgage_inspection));
SELECT pg_temp.expect('mortgage_inspection_party', (SELECT count(*) FROM mortgage_inspection_parties), (SELECT count(*) FROM sister_next.mortgage_inspection_party));
SELECT pg_temp.expect('mortgage_inspection_property', (SELECT count(*) FROM mortgage_inspection_properties), (SELECT count(*) FROM sister_next.mortgage_inspection_property));

-- ---- entities where equal keys were merged: the new count is the number of distinct normalised keys ---------
SELECT pg_temp.expect('every location mapped', (SELECT count(*) FROM cadastral_locations), (SELECT count(*) FROM unit_map));
SELECT pg_temp.expect('cadastral_unit (+ mortgage units created)', (SELECT count(DISTINCT new_id) FROM unit_map),
       (SELECT count(*) FROM sister_next.cadastral_unit) - (SELECT count(*) FROM sister_next.cadastral_unit u
                                                            WHERE NOT EXISTS (SELECT 1 FROM unit_map m WHERE m.new_id = u.id)));
SELECT pg_temp.expect('right_kind', (SELECT count(DISTINCT new_id) FROM rk_map), (SELECT count(*) FROM sister_next.right_kind));
SELECT pg_temp.expect('ownership_right', (SELECT count(DISTINCT new_id) FROM or_map), (SELECT count(*) FROM sister_next.ownership_right));
SELECT pg_temp.expect('result_owner',
       (SELECT count(*) FROM (SELECT DISTINCT rm.id, o.result_id, o.subject_id, orm.new_id
                              FROM visura_owners o JOIN req_map rm ON rm.request_ref = o.response_id
                              LEFT JOIN or_map orm ON orm.old_id = o.right_id) x),
       (SELECT count(*) FROM sister_next.result_owner));
SELECT pg_temp.expect('mutation_owner',
       (SELECT count(*) FROM (SELECT DISTINCT o.mutation_id, o.subject_id, orm.new_id
                              FROM property_owners o LEFT JOIN or_map orm ON orm.old_id = o.right_id) x),
       (SELECT count(*) FROM sister_next.mutation_owner));
SELECT pg_temp.expect('subject (+ mortgage parties)', (SELECT count(*) FROM cadastral_subjects) + (SELECT count(*) FROM _party_key WHERE is_new),
       (SELECT count(*) FROM sister_next.subject));
SELECT pg_temp.expect('subject ids kept', 0, (SELECT count(*) FROM cadastral_subjects s WHERE NOT EXISTS (SELECT 1 FROM sister_next.subject n WHERE n.id = s.id)));
SELECT pg_temp.expect('building_unit_related_parcel',
       (SELECT count(*) FROM (SELECT DISTINCT coalesce(r.building_unit_id, cur.id), um.new_id, r.sequence_id
                              FROM related_parcels r JOIN unit_map um ON um.old_id = r.location_id
                              LEFT JOIN sister_next.building_unit cur ON cur.is_current AND cur.document_id = r.current_state_id) x),
       (SELECT count(*) FROM sister_next.building_unit_related_parcel));
SELECT pg_temp.expect('parcel_geometry_cache',
       (SELECT count(*) FROM (SELECT DISTINCT g.feature_type,
                                     coalesce(pg_temp.comune_by_code(g.municipality_code), pg_temp.comune_id(g.municipality, g.province)),
                                     g.section, g.sheet, g.parcel FROM cadastral_map_geometry_cache g) x),
       (SELECT count(*) FROM sister_next.parcel_geometry_cache));

-- ---- no link lost: NULL-ness of every foreign reference is unchanged ---------------------------------------
SELECT pg_temp.expect('request.cadastral_unit_id nulls', (SELECT count(*) FROM visura_requests WHERE location_id IS NULL), (SELECT count(*) FROM sister_next.request WHERE cadastral_unit_id IS NULL));
SELECT pg_temp.expect('property.cadastral_unit_id nulls', (SELECT count(*) FROM visura_properties WHERE location_id IS NULL), (SELECT count(*) FROM sister_next.property WHERE cadastral_unit_id IS NULL));
SELECT pg_temp.expect('property.subject_id nulls', (SELECT count(*) FROM visura_properties WHERE subject_id IS NULL), (SELECT count(*) FROM sister_next.property WHERE subject_id IS NULL));
SELECT pg_temp.expect('property.result_id nulls', (SELECT count(*) FROM visura_properties WHERE result_id IS NULL), (SELECT count(*) FROM sister_next.property WHERE result_id IS NULL));
SELECT pg_temp.expect('document.request_id nulls', (SELECT count(*) FROM visura_documents WHERE response_id IS NULL), (SELECT count(*) FROM sister_next.document WHERE request_id IS NULL));
SELECT pg_temp.expect('document.cadastral_unit_id', (SELECT count(*) FROM document_metadata WHERE location_id IS NOT NULL), (SELECT count(*) FROM sister_next.document WHERE cadastral_unit_id IS NOT NULL));
SELECT pg_temp.expect('result_owner.right_id nulls', (SELECT count(*) FROM visura_owners WHERE right_id IS NULL), (SELECT count(*) FROM sister_next.result_owner WHERE right_id IS NULL));
SELECT pg_temp.expect('subject.birth_comune', (SELECT count(*) FROM cadastral_subjects WHERE birth_place_id IS NOT NULL OR nullif(birth_municipality_code, '') IS NOT NULL),
       (SELECT count(*) FROM sister_next.subject s WHERE s.id <= (SELECT max(id) FROM cadastral_subjects) AND s.birth_comune_id IS NOT NULL));
SELECT pg_temp.expect('building_unit.cadastral_unit_id', (SELECT count(*) FROM building_identifiers WHERE location_id IS NOT NULL), (SELECT count(*) FROM sister_next.building_unit WHERE cadastral_unit_id IS NOT NULL));
SELECT pg_temp.expect('ownership_mutation.reference unit', (SELECT count(*) FROM ownership_mutations WHERE reference_location_id IS NOT NULL), (SELECT count(*) FROM sister_next.ownership_mutation WHERE reference_cadastral_unit_id IS NOT NULL));
SELECT pg_temp.expect('property_group.comune', (SELECT count(*) FROM property_groups WHERE nullif(btrim(municipality_code), '') IS NOT NULL), (SELECT count(*) FROM sister_next.property_group WHERE comune_id IS NOT NULL));
SELECT pg_temp.expect('geometry cache comune resolved', 0, (SELECT count(*) FROM cadastral_map_geometry_cache g
       WHERE coalesce(pg_temp.comune_by_code(g.municipality_code), pg_temp.comune_id(g.municipality, g.province)) IS NULL));

-- ---- totals of the typed numbers equal the totals of the text they came from -------------------------------
SELECT pg_temp.expect('sum property.income_eur',
       (SELECT coalesce(sum(replace(substring(income from '[0-9]+(?:,[0-9]+)?'), ',', '.')::numeric), 0) FROM visura_properties WHERE income !~ 'R\.D\.' AND income ~ '[0-9]'),
       (SELECT coalesce(sum(income_eur), 0) FROM sister_next.property));
SELECT pg_temp.expect('sum property.area_sqm', (SELECT coalesce(sum(area::numeric), 0) FROM visura_properties WHERE area ~ '^\d+(\.\d+)?$'), (SELECT coalesce(sum(area_sqm), 0) FROM sister_next.property));
SELECT pg_temp.expect('sum property.dominical_income_eur',
       (SELECT coalesce(sum(coalesce(nullif(replace(dominical_income, ',', '.'), '')::numeric,
                                     replace(substring(income from 'R\.D\.\s*Euro:\s*([0-9.,]+)'), ',', '.')::numeric)), 0) FROM visura_properties),
       (SELECT coalesce(sum(dominical_income_eur), 0) FROM sister_next.property));
SELECT pg_temp.expect('count property consistency VANI', (SELECT count(*) FROM visura_properties WHERE consistency ~* 'vani'), (SELECT count(*) FROM sister_next.property WHERE consistency_unit = 'VANI'));
SELECT pg_temp.expect('count property consistency MQ', (SELECT count(*) FROM visura_properties WHERE consistency ~* 'm\s*2|mq' AND consistency !~* 'vani'), (SELECT count(*) FROM sister_next.property WHERE consistency_unit = 'MQ'));
SELECT pg_temp.expect('sum land area', (SELECT coalesce(sum(area), 0) FROM land_classifications), (SELECT coalesce(sum(area_sqm), 0) FROM sister_next.land_parcel_classification));
SELECT pg_temp.expect('sum land dominical income', (SELECT coalesce(sum(dominical_income), 0) FROM land_classifications), (SELECT coalesce(sum(dominical_income_eur), 0) FROM sister_next.land_parcel_classification));
SELECT pg_temp.expect('sum building cadastral income', (SELECT coalesce(sum(cadastral_income), 0) FROM building_classifications), (SELECT coalesce(sum(cadastral_income_eur), 0) FROM sister_next.building_unit));
SELECT pg_temp.expect('sum building total area', (SELECT coalesce(sum(total_area), 0) FROM building_surfaces), (SELECT coalesce(sum(total_area_sqm), 0) FROM sister_next.building_unit));
SELECT pg_temp.expect('sum document.file_size', (SELECT coalesce(sum(file_size), 0) FROM visura_documents), (SELECT coalesce(sum(file_size), 0) FROM sister_next.document));
SELECT pg_temp.expect('document.file_name is the basename of file_path', 0, (SELECT count(*) FROM visura_documents WHERE regexp_replace(file_path, '^.*/', '') <> filename));
SELECT pg_temp.expect('subjects with a birth date', (SELECT count(*) FROM cadastral_subjects WHERE nullif(btrim(date_of_birth), '') IS NOT NULL),
       (SELECT count(*) FROM sister_next.subject s WHERE s.id <= (SELECT max(id) FROM cadastral_subjects) AND s.birth_date IS NOT NULL));
SELECT pg_temp.expect('rights with a share', (SELECT count(*) FROM ownership_rights WHERE ownership_share ~ '^\s*\d+\s*/\s*\d+\s*$'),
       (SELECT count(*) FROM ownership_rights r JOIN sister_next.ownership_right n ON n.id = (SELECT new_id FROM or_map WHERE old_id = r.id)
        WHERE r.ownership_share ~ '^\s*\d+\s*/\s*\d+\s*$' AND n.share_numerator IS NOT NULL));

-- ---- what the dropped columns carried is still available elsewhere ------------------------------------------
SELECT pg_temp.expect('page_visit *_json had no data beyond the child rows', 0,
       (SELECT count(*) FROM page_visits v WHERE (v.form_elements_json IS NOT NULL AND v.form_elements_json NOT IN ('[]', '')
                                                 AND NOT EXISTS (SELECT 1 FROM page_visit_form_elements f WHERE f.page_visit_id = v.id))
                                              OR (v.errors_json IS NOT NULL AND v.errors_json NOT IN ('[]', '')
                                                 AND NOT EXISTS (SELECT 1 FROM page_visit_errors e WHERE e.page_visit_id = v.id))));
SELECT pg_temp.expect('activity_log params/result flattened', (SELECT count(*) FROM activity_log WHERE params IS NOT NULL OR result IS NOT NULL),
       (SELECT count(DISTINCT activity_id) FROM sister_next.activity_log_attribute));
SELECT pg_temp.expect('archive request_payload', (SELECT count(*) FROM visura_responses WHERE data IS NOT NULL), (SELECT count(*) FROM sister_archive.request_payload));
SELECT pg_temp.expect('archive document_content', (SELECT count(*) FROM document_metadata WHERE content IS NOT NULL), (SELECT count(*) FROM sister_archive.document_content));
SELECT pg_temp.expect('archive xml nodes', (SELECT count(*) FROM document_xml_nodes), (SELECT count(*) FROM sister_archive.document_xml_node));
SELECT pg_temp.expect('archive xml attributes', (SELECT count(*) FROM document_xml_attributes), (SELECT count(*) FROM sister_archive.document_xml_attribute));
SELECT pg_temp.expect('archive extraction payloads', (SELECT count(*) FROM document_structured_extractions), (SELECT count(*) FROM sister_archive.document_extraction_payload));
SELECT pg_temp.expect('archive activity payloads', (SELECT count(*) FROM activity_log), (SELECT count(*) FROM sister_archive.activity_log_payload));

-- ---- text -> integer casts lose nothing: every non-blank source is numeric, or is a known marker ------------------
SELECT pg_temp.expect('mutation_index is numeric or the "current" marker', 0,
       (SELECT count(*) FROM ownership_mutations WHERE mutation_index IS NOT NULL AND mutation_index !~ '^\d+$' AND mutation_index <> 'current'));
SELECT pg_temp.expect('mutation is_current', (SELECT count(*) FROM ownership_mutations WHERE mutation_index = 'current'),
       (SELECT count(*) FROM sister_next.ownership_mutation WHERE is_current));
SELECT pg_temp.expect('mutation_index numeric', (SELECT count(*) FROM ownership_mutations WHERE mutation_index ~ '^\d+$'),
       (SELECT count(*) FROM sister_next.ownership_mutation WHERE mutation_index IS NOT NULL));
SELECT pg_temp.expect('group_index numeric', 0, (SELECT count(*) FROM property_groups WHERE nullif(btrim(group_index), '') IS NOT NULL AND btrim(group_index) !~ '^\d+$'));
SELECT pg_temp.expect('unit_index numeric', 0, (SELECT count(*) FROM building_units WHERE nullif(btrim(unit_index), '') IS NOT NULL AND btrim(unit_index) !~ '^\d+$'));
SELECT pg_temp.expect('owner_index numeric', 0, (SELECT count(*) FROM property_owners WHERE nullif(btrim(owner_index), '') IS NOT NULL AND btrim(owner_index) !~ '^\d+$'));
SELECT pg_temp.expect('document protocol_year/liquidation_year numeric', 0,
       (SELECT count(*) FROM document_metadata WHERE (nullif(btrim(year), '') IS NOT NULL AND btrim(year) !~ '^\d{4}$')
                                                   OR (nullif(btrim(liquidation_year), '') IS NOT NULL AND btrim(liquidation_year) !~ '^\d{4}$')));

-- ---- Belfiore codes carried by the old rows are still reachable through the unit's comune --------------------------
SELECT pg_temp.expect('municipality_code preserved on ' || s, 0, (
    SELECT count(*) FROM (
        SELECT 'building_identifiers' AS s, bi.location_id AS loc, btrim(bi.municipality_code) AS code FROM building_identifiers bi
        UNION ALL SELECT 'land_parcels', p.location_id, btrim(p.municipality_code) FROM land_parcels p
        UNION ALL SELECT 'document_metadata', m.location_id, btrim(m.municipality_code) FROM document_metadata m
        UNION ALL SELECT 'ownership_mutations', m.reference_location_id, btrim(m.ref_municipality_code) FROM ownership_mutations m
    ) x
    LEFT JOIN unit_map um ON um.old_id = x.loc
    LEFT JOIN sister_next.cadastral_unit u ON u.id = um.new_id
    LEFT JOIN sister_next.comune c ON c.id = u.comune_id
    WHERE nullif(x.code, '') IS NOT NULL AND x.s = s AND c.cadastral_code IS DISTINCT FROM x.code))
FROM (VALUES ('building_identifiers'), ('land_parcels'), ('document_metadata'), ('ownership_mutations')) AS t (s);

-- ---- structural invariants ------------------------------------------------------------------------------------
SELECT pg_temp.expect('mutations with no document', 0, (SELECT count(*) FROM sister_next.ownership_mutation WHERE document_id IS NULL));
SELECT pg_temp.expect('documents with more than one current building state', 0,
       (SELECT count(*) FROM (SELECT document_id FROM sister_next.building_unit WHERE is_current GROUP BY 1 HAVING count(*) > 1) x));

-- Source values that could not be typed are listed, not fatal: nothing is lost, they stay in the legacy schema.
DO $$
DECLARE n bigint;
BEGIN
    SELECT count(*) INTO n FROM sister_archive.migration_unparsed;
    IF n > 0 THEN
        RAISE NOTICE '% source values could not be typed; see sister_archive.migration_unparsed', n;
    END IF;
END $$;
