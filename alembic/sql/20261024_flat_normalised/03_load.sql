-- Step 3: fill sister_next from the (deduplicated) legacy tables in `sister`.
-- Old ids are kept wherever a table maps 1:1, so rows stay traceable; merged rows go through *_map temp tables.
SET LOCAL search_path = sister, public;

-- ---------------------------------------------------------------------------------------------------------
-- Parsers for the portal's text formats
-- ---------------------------------------------------------------------------------------------------------
CREATE FUNCTION pg_temp.it_date(t text) RETURNS date LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF t ~ '^\s*\d{2}/\d{2}/\d{4}\s*$' THEN RETURN to_date(btrim(t), 'DD/MM/YYYY'); END IF;
    IF t ~ '^\s*\d{4}-\d{2}-\d{2}' THEN RETURN left(btrim(t), 10)::date; END IF;
    IF t ~ '^\s*\d{8}\s*$' THEN RETURN to_date(btrim(t), 'YYYYMMDD'); END IF;
    RETURN NULL;
EXCEPTION WHEN others THEN RETURN NULL;
END $$;

-- 'dd/mm/yyyy hh:mi:ss' (or an ISO date, taken as midnight) in Italian local time; stray non-breaking spaces tolerated
CREATE FUNCTION pg_temp.it_ts(t text) RETURNS timestamptz LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE m text[]; c text := btrim(regexp_replace(t, '[^0-9/:-]+', ' ', 'g'));
BEGIN
    m := regexp_match(c, '^(\d{2})/(\d{2})/(\d{4}) (\d{2}):(\d{2}):(\d{2})$');
    IF m IS NOT NULL THEN
        RETURN make_timestamptz(m[3]::int, m[2]::int, m[1]::int, m[4]::int, m[5]::int, m[6]::double precision, 'Europe/Rome');
    END IF;
    m := regexp_match(c, '^(\d{4})-(\d{2})-(\d{2})$');
    IF m IS NOT NULL THEN
        RETURN make_timestamptz(m[1]::int, m[2]::int, m[3]::int, 0, 0, 0, 'Europe/Rome');
    END IF;
    RETURN NULL;
EXCEPTION WHEN others THEN RETURN NULL;
END $$;

-- 'YYYYMMDD' + 'HHMMSS' from the XML header, Italian local time
CREATE FUNCTION pg_temp.compact_ts(d text, t text) RETURNS timestamptz LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF d !~ '^\d{8}$' THEN RETURN NULL; END IF;
    RETURN make_timestamptz(substr(d, 1, 4)::int, substr(d, 5, 2)::int, substr(d, 7, 2)::int,
                            CASE WHEN t ~ '^\d{6}$' THEN substr(t, 1, 2)::int ELSE 0 END,
                            CASE WHEN t ~ '^\d{6}$' THEN substr(t, 3, 2)::int ELSE 0 END,
                            CASE WHEN t ~ '^\d{6}$' THEN substr(t, 5, 2)::int ELSE 0 END, 'Europe/Rome');
EXCEPTION WHEN others THEN RETURN NULL;
END $$;

-- first number in a string, decimal comma or point: 'R.Euro:1704,31' -> 1704.31, '9.9 vani' -> 9.9
CREATE FUNCTION pg_temp.it_num(t text) RETURNS numeric LANGUAGE sql IMMUTABLE AS $$
    SELECT replace(substring(t from '[0-9]+(?:[.,][0-9]+)?'), ',', '.')::numeric $$;

-- provinces appear as codes ('PA') and as names ('PALERMO'); NAZIONALE is a portal office level, not a province
CREATE FUNCTION pg_temp.prov(t text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE upper(btrim(t))
        WHEN 'AGRIGENTO' THEN 'AG' WHEN 'ANCONA' THEN 'AN' WHEN 'BARI' THEN 'BA' WHEN 'BERGAMO' THEN 'BG'
        WHEN 'BOLOGNA' THEN 'BO' WHEN 'BRESCIA' THEN 'BS' WHEN 'GENOVA' THEN 'GE' WHEN 'NAPOLI' THEN 'NA'
        WHEN 'PALERMO' THEN 'PA' WHEN 'RAVENNA' THEN 'RA' WHEN 'ROMA' THEN 'RM' WHEN 'TRIESTE' THEN 'TS'
        ELSE CASE WHEN upper(btrim(t)) ~ '^[A-Z]{2}$' THEN upper(btrim(t)) END
    END $$;

-- place names: upper case, trimmed, 'NATO IN BRASILE' (foreign birthplace) -> 'BRASILE'
CREATE FUNCTION pg_temp.cname(n text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT nullif(upper(btrim(regexp_replace(n, '^\s*NAT[AO]\s+(IN|A)\s+', '', 'i'))), '') $$;

-- 'A02' / 'A/2: ABITAZIONI ...' -> 'A/2'
CREATE FUNCTION pg_temp.norm_category(c text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN btrim(c) ~ '^[A-Z]/[0-9]+' THEN substring(btrim(c) from '^([A-Z]/[0-9]+)')
        WHEN btrim(c) ~ '^[A-Z]0*[0-9]+$' THEN substr(btrim(c), 1, 1) || '/' || ltrim(substr(btrim(c), 2), '0')
    END $$;

-- '01' -> '1'; 'U' stays
CREATE FUNCTION pg_temp.norm_class(c text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT NULLIF(CASE WHEN btrim(c) ~ '^[0-9]+$' THEN ltrim(btrim(c), '0') ELSE btrim(c) END, '') $$;

CREATE FUNCTION pg_temp.consistency_unit(c text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN c ~* 'vani' THEN 'VANI' WHEN c ~* 'm\s*2|mq' THEN 'MQ' WHEN c ~* 'mc|m\s*3' THEN 'MC' END $$;

-- ---------------------------------------------------------------------------------------------------------
-- comune: every place name seen anywhere, one row per (province, name), with its Belfiore code when unambiguous
-- ---------------------------------------------------------------------------------------------------------
CREATE TEMP TABLE _c_src (name text, prov text, code text) ON COMMIT DROP;
INSERT INTO _c_src SELECT pg_temp.cname(municipality), pg_temp.prov(province), NULL
FROM cadastral_locations WHERE nullif(btrim(municipality), '') IS NOT NULL;
INSERT INTO _c_src SELECT pg_temp.cname(municipality), pg_temp.prov(province), NULL
FROM geographic_places WHERE nullif(btrim(municipality), '') IS NOT NULL;
INSERT INTO _c_src SELECT pg_temp.cname(g.municipality), pg_temp.prov(g.province), nullif(btrim(s.birth_municipality_code), '')
FROM cadastral_subjects s JOIN geographic_places g ON g.id = s.birth_place_id
WHERE nullif(btrim(s.birth_municipality_code), '') IS NOT NULL;
INSERT INTO _c_src SELECT pg_temp.cname(municipality), pg_temp.prov(province), nullif(btrim(municipality_code), '')
FROM cadastral_map_geometry_cache WHERE nullif(btrim(municipality), '') IS NOT NULL;
INSERT INTO _c_src SELECT pg_temp.cname(l.municipality), pg_temp.prov(l.province), nullif(btrim(bi.municipality_code), '')
FROM building_identifiers bi JOIN cadastral_locations l ON l.id = bi.location_id
WHERE nullif(btrim(bi.municipality_code), '') IS NOT NULL AND nullif(btrim(l.municipality), '') IS NOT NULL;
INSERT INTO _c_src SELECT pg_temp.cname(l.municipality), pg_temp.prov(l.province), nullif(btrim(m.municipality_code), '')
FROM document_metadata m JOIN cadastral_locations l ON l.id = m.location_id
WHERE nullif(btrim(m.municipality_code), '') IS NOT NULL AND nullif(btrim(l.municipality), '') IS NOT NULL;
INSERT INTO _c_src SELECT pg_temp.cname(l.municipality), pg_temp.prov(l.province), nullif(btrim(g.municipality_code), '')
FROM property_groups g
JOIN building_units u ON u.group_id = g.id
JOIN building_identifiers bi ON bi.building_unit_id = u.id
JOIN cadastral_locations l ON l.id = bi.location_id
WHERE nullif(btrim(g.municipality_code), '') IS NOT NULL AND nullif(btrim(l.municipality), '') IS NOT NULL;
-- mortgage properties spell the municipality as 'RAVENNA (RA)' or 'RAVENNA(RA)'
INSERT INTO _c_src SELECT pg_temp.cname(coalesce(m[1], municipality)), pg_temp.prov(m[2]), nullif(btrim(municipality_code), '')
FROM (SELECT municipality, municipality_code, regexp_match(municipality, '^\s*(.*?)\s*\(([A-Za-z]{2})\)\s*$') AS m
      FROM mortgage_inspection_properties) x WHERE nullif(btrim(municipality), '') IS NOT NULL;
INSERT INTO _c_src SELECT pg_temp.cname(m[1]), pg_temp.prov(m[2]), NULL
FROM (SELECT regexp_match(registered_office, '^\s*(.*?)\s*\(([A-Za-z]{2})\)\s*$') AS m FROM visura_properties) x WHERE m IS NOT NULL;
INSERT INTO _c_src SELECT pg_temp.cname(m[1]), pg_temp.prov(m[2]), NULL
FROM (SELECT regexp_match(birth_place, '^\s*(.*?)\s*\(([A-Za-z]{2})\)\s*$') AS m FROM mortgage_inspection_parties) x WHERE m IS NOT NULL;

-- some sources put the Belfiore code where the name belongs ('H199'): that is a code, not a place name
DELETE FROM _c_src WHERE name ~ '^[A-Z][0-9]{3}$';

-- a name that is only ever seen with one province gets that province where the source left it blank
CREATE TEMP TABLE _c_fix ON COMMIT DROP AS
SELECT name, min(prov) AS prov FROM _c_src WHERE prov IS NOT NULL GROUP BY name HAVING count(DISTINCT prov) = 1;
-- same comune recorded under its old and its current province (Monza: MI -> MB); filled once the codes are known
CREATE TEMP TABLE _c_alias (name text, prov text, canon_prov text) ON COMMIT DROP;
CREATE FUNCTION pg_temp.eff_prov(n text, p text) RETURNS text LANGUAGE sql STABLE AS $$
    SELECT coalesce(a.canon_prov, b.prov)
    FROM (SELECT coalesce(pg_temp.prov(p), (SELECT f.prov FROM _c_fix f WHERE f.name = pg_temp.cname(n))) AS prov) b
    LEFT JOIN _c_alias a ON a.name = pg_temp.cname(n) AND a.prov IS NOT DISTINCT FROM b.prov $$;

CREATE TEMP TABLE _c_norm ON COMMIT DROP AS SELECT name, pg_temp.eff_prov(name, prov) AS prov, code FROM _c_src;

-- Codes the data references but never names (e.g. property groups without units): name them from ISTAT if it is available.
CREATE TEMP TABLE _seen_codes ON COMMIT DROP AS
SELECT DISTINCT pg_temp.cname(c) AS code FROM (
    SELECT municipality_code AS c FROM property_groups UNION ALL SELECT municipality_code FROM document_metadata
    UNION ALL SELECT municipality_code FROM building_identifiers UNION ALL SELECT municipality_code FROM cadastral_map_geometry_cache
    UNION ALL SELECT municipality_code FROM mortgage_inspection_properties UNION ALL SELECT birth_municipality_code FROM cadastral_subjects
) x WHERE nullif(btrim(c), '') IS NOT NULL;
CREATE TEMP TABLE _istat (name text, prov text, code text) ON COMMIT DROP;
DO $$
BEGIN
    IF to_regclass('istat.municipalities') IS NOT NULL THEN
        -- ISTAT writes 'FORLÌ', the portal writes "FORLI'"
        EXECUTE $q$INSERT INTO _istat
                    SELECT replace(replace(replace(replace(replace(replace(pg_temp.cname(name),
                               'À', 'A'''), 'È', 'E'''), 'É', 'E'''), 'Ì', 'I'''), 'Ò', 'O'''), 'Ù', 'U'''),
                           pg_temp.cname(province_abbreviation), pg_temp.cname(cadastral_code)
                    FROM istat.municipalities WHERE nullif(btrim(cadastral_code), '') IS NOT NULL$q$;
    END IF;
END $$;
INSERT INTO _c_norm SELECT i.name, i.prov, i.code FROM _istat i
WHERE i.code IN (SELECT code FROM _seen_codes) AND i.code NOT IN (SELECT code FROM _c_norm WHERE code IS NOT NULL);

-- One code per comune. Where the data gives a code to several names, ISTAT decides; without ISTAT the code stays unassigned.
CREATE TEMP TABLE _cand ON COMMIT DROP AS SELECT DISTINCT name, prov, code FROM _c_norm WHERE code IS NOT NULL;
CREATE TEMP TABLE _c_code ON COMMIT DROP AS
SELECT c.name, c.prov, c.code FROM _cand c
WHERE (SELECT count(*) FROM _cand c2 WHERE c2.code = c.code) = 1
   OR EXISTS (SELECT 1 FROM _istat i WHERE i.code = c.code AND i.name = c.name AND i.prov IS NOT DISTINCT FROM c.prov);
DELETE FROM _c_code WHERE code IN (SELECT code FROM _c_code GROUP BY code HAVING count(*) > 1);
DELETE FROM _c_code WHERE (name, prov) IN (SELECT name, prov FROM _c_code GROUP BY name, prov HAVING count(*) > 1);

INSERT INTO _c_alias
SELECT d.name, d.prov, o.prov
FROM (SELECT DISTINCT name, prov FROM _c_norm) d
JOIN LATERAL (SELECT min(code) AS code FROM _istat i WHERE i.name = d.name HAVING count(*) = 1) u ON true
JOIN _c_code o ON o.code = u.code AND o.name = d.name AND o.prov IS DISTINCT FROM d.prov
WHERE NOT EXISTS (SELECT 1 FROM _c_code c WHERE c.name = d.name AND c.prov IS NOT DISTINCT FROM d.prov);
UPDATE _c_norm n SET prov = a.canon_prov FROM _c_alias a WHERE n.name = a.name AND n.prov IS NOT DISTINCT FROM a.prov;

-- A comune still without a code takes ISTAT's when (name, province) match, or when the name is unique in ISTAT
-- (provinces were reorganised: Monza e Brianza, Sud Sardegna, Olbia-Tempio...).
INSERT INTO _c_code
SELECT d.name, d.prov, coalesce(i1.code, i2.code)
FROM (SELECT DISTINCT name, prov FROM _c_norm) d
LEFT JOIN _c_code c ON c.name = d.name AND c.prov IS NOT DISTINCT FROM d.prov
LEFT JOIN _istat i1 ON i1.name = d.name AND i1.prov = d.prov
LEFT JOIN LATERAL (SELECT min(code) AS code FROM _istat i WHERE i.name = d.name HAVING count(*) = 1) i2 ON true
WHERE c.code IS NULL AND coalesce(i1.code, i2.code) IS NOT NULL
  AND coalesce(i1.code, i2.code) NOT IN (SELECT code FROM _c_code);

INSERT INTO sister_next.comune (name, province_code, cadastral_code)
SELECT d.name, d.prov, c.code
FROM (SELECT DISTINCT name, prov FROM _c_norm) d
LEFT JOIN _c_code c ON c.name = d.name AND c.prov IS NOT DISTINCT FROM d.prov
ORDER BY d.name, d.prov;

CREATE FUNCTION pg_temp.comune_id(n text, p text) RETURNS bigint LANGUAGE sql STABLE AS $$
    SELECT c.id FROM sister_next.comune c
    WHERE nullif(btrim(n), '') IS NOT NULL AND c.name = pg_temp.cname(n) AND c.province_code IS NOT DISTINCT FROM pg_temp.eff_prov(n, p) $$;
CREATE FUNCTION pg_temp.comune_by_code(code text) RETURNS bigint LANGUAGE sql STABLE AS $$
    SELECT c.id FROM sister_next.comune c WHERE c.cadastral_code = nullif(btrim(code), '') $$;

-- ---------------------------------------------------------------------------------------------------------
-- cadastral_unit (merging places that only differed by province spelling or empty-vs-NULL)
-- ---------------------------------------------------------------------------------------------------------
CREATE TEMP TABLE _unit_key ON COMMIT DROP AS
SELECT l.id, nullif(btrim(l.cadastre_type), '') AS ct, pg_temp.comune_id(l.municipality, l.province) AS cid,
       l.section, l.sheet, l.parcel, l.subunit
FROM cadastral_locations l;
CREATE TEMP TABLE unit_map ON COMMIT DROP AS
SELECT id AS old_id, min(id) OVER (PARTITION BY ct, cid, section, sheet, parcel, subunit) AS new_id FROM _unit_key;
INSERT INTO sister_next.cadastral_unit (id, cadastre_type, comune_id, section, sheet, parcel, subunit)
SELECT k.id, k.ct, k.cid, k.section, k.sheet, k.parcel, k.subunit FROM _unit_key k WHERE k.id IN (SELECT new_id FROM unit_map);

-- ---------------------------------------------------------------------------------------------------------
-- subject
-- ---------------------------------------------------------------------------------------------------------
INSERT INTO sister_next.subject (id, subject_type, fiscal_code, name, last_name, first_name, gender, birth_date, birth_comune_id)
SELECT s.id,
       nullif(s.subject_type, ''),
       nullif(upper(regexp_replace(s.fiscal_code, '\s', '', 'g')), ''),
       coalesce(nullif(btrim(s.display_name), ''), nullif(btrim(concat_ws(' ', s.last_name, s.first_name)), ''),
                nullif(upper(regexp_replace(s.fiscal_code, '\s', '', 'g')), ''), '(sconosciuto)'),
       nullif(btrim(s.last_name), ''), nullif(btrim(s.first_name), ''), nullif(s.gender, ''),
       pg_temp.it_date(s.date_of_birth),
       coalesce(pg_temp.comune_id(g.municipality, g.province), pg_temp.comune_by_code(s.birth_municipality_code))
FROM cadastral_subjects s LEFT JOIN geographic_places g ON g.id = s.birth_place_id;

-- ---------------------------------------------------------------------------------------------------------
-- rights: kind + share (reduced fraction) + period
-- ---------------------------------------------------------------------------------------------------------
CREATE TEMP TABLE _rk ON COMMIT DROP AS
SELECT id, nullif(btrim(right_type), '') AS t, nullif(btrim(right_code), '') AS c, nullif(btrim(right_description), '') AS d
FROM ownership_right_catalog;
CREATE TEMP TABLE rk_map ON COMMIT DROP AS
SELECT id AS old_id, min(id) OVER (PARTITION BY t, c, d) AS new_id FROM _rk;
INSERT INTO sister_next.right_kind (id, right_type, right_code, description)
SELECT id, t, c, d FROM _rk WHERE id IN (SELECT new_id FROM rk_map);

CREATE TEMP TABLE _or ON COMMIT DROP AS
SELECT r.id, km.new_id AS kind_id,
       CASE WHEN f.den > 0 THEN f.num / gcd(f.num, f.den) END AS num,
       CASE WHEN f.den > 0 THEN f.den / gcd(f.num, f.den) END AS den,
       pg_temp.it_date(r.start_date) AS sd, pg_temp.it_date(r.end_date) AS ed
FROM ownership_rights r
JOIN rk_map km ON km.old_id = r.right_catalog_id
CROSS JOIN LATERAL (
    SELECT CASE WHEN r.ownership_share ~ '^\s*\d{1,9}\s*/\s*\d{1,9}\s*$' THEN split_part(btrim(r.ownership_share), '/', 1)::bigint END AS num,
           CASE WHEN r.ownership_share ~ '^\s*\d{1,9}\s*/\s*\d{1,9}\s*$' THEN split_part(btrim(r.ownership_share), '/', 2)::bigint END AS den
) f;
CREATE TEMP TABLE or_map ON COMMIT DROP AS
SELECT id AS old_id, min(id) OVER (PARTITION BY kind_id, num, den, sd, ed) AS new_id FROM _or;
INSERT INTO sister_next.ownership_right (id, right_kind_id, share_numerator, share_denominator, start_date, end_date)
SELECT id, kind_id, num, den, sd, ed FROM _or WHERE id IN (SELECT new_id FROM or_map);

-- ---------------------------------------------------------------------------------------------------------
-- Lookups
-- ---------------------------------------------------------------------------------------------------------
INSERT INTO sister_next.request_type SELECT DISTINCT btrim(request_type) FROM visura_requests WHERE nullif(btrim(request_type), '') IS NOT NULL;
INSERT INTO sister_next.document_type SELECT DISTINCT btrim(document_type) FROM visura_documents WHERE nullif(btrim(document_type), '') IS NOT NULL;
INSERT INTO sister_next.cadastral_category (code, description)
SELECT code, max(description) FROM (
    SELECT pg_temp.norm_category(category) AS code, nullif(btrim(substring(category from ':\s*(.*)$')), '') AS description
    FROM building_classifications
    UNION ALL SELECT pg_temp.norm_category(category), nullif(btrim(substring(category from ':\s*(.*)$')), '') FROM visura_properties
) x WHERE code IS NOT NULL GROUP BY code;
INSERT INTO sister_next.land_quality
SELECT DISTINCT upper(btrim(q)) FROM (SELECT quality AS q FROM visura_properties UNION ALL SELECT quality FROM land_classifications) x
WHERE nullif(btrim(q), '') IS NOT NULL;
INSERT INTO sister_next.activity_action SELECT DISTINCT action FROM activity_log;
INSERT INTO sister_next.mortgage_lien_type SELECT DISTINCT lien_type FROM mortgage_inspection_liens WHERE nullif(lien_type, '') IS NOT NULL;
INSERT INTO sister_next.mortgage_title_type SELECT DISTINCT title_type FROM mortgage_inspection_titles WHERE nullif(title_type, '') IS NOT NULL;
INSERT INTO sister_next.mortgage_note_type SELECT DISTINCT note_type FROM mortgage_inspections WHERE nullif(note_type, '') IS NOT NULL;
INSERT INTO sister_next.mortgage_party_quality SELECT DISTINCT quality FROM mortgage_inspection_parties WHERE nullif(quality, '') IS NOT NULL;
INSERT INTO sister_next.land_registry_office (name)
SELECT DISTINCT btrim(source_system) FROM document_metadata WHERE nullif(btrim(source_system), '') IS NOT NULL;

-- ---------------------------------------------------------------------------------------------------------
-- request (+ response), results, properties, owners
-- ---------------------------------------------------------------------------------------------------------
INSERT INTO sister_next.request (request_ref, request_type, cadastral_unit_id, cache_key, created_at, responded_at, success, error,
                                 cadastre_type, total_results, total_intestati, skipped_soppresso, subject_query)
SELECT q.request_id, nullif(btrim(q.request_type), ''), um.new_id, q.cache_key, q.created_at, r.created_at, r.success,
       nullif(r.error, ''), nullif(btrim(r.cadastre_type), ''), r.total_results, r.total_intestati, r.skipped_soppresso,
       nullif(r.subject_query, '')
FROM visura_requests q
LEFT JOIN visura_responses r ON r.request_id = q.request_id
LEFT JOIN unit_map um ON um.old_id = q.location_id
ORDER BY q.created_at, q.request_id;
CREATE TEMP TABLE req_map ON COMMIT DROP AS SELECT request_ref, id FROM sister_next.request;

INSERT INTO sister_archive.request_payload (request_id, data)
SELECT rm.id, r.data FROM visura_responses r JOIN req_map rm ON rm.request_ref = r.request_id WHERE r.data IS NOT NULL;

INSERT INTO sister_next.request_result (id, request_id, result_index, visura_present)
SELECT r.id, rm.id, r.result_index, r.visura_present FROM visura_results r JOIN req_map rm ON rm.request_ref = r.response_id;

INSERT INTO sister_next.property (id, request_id, result_id, property_type, cadastral_unit_id, subject_id, address, partita, category,
                                  cadastral_class, consistency_value, consistency_unit, census_zone, land_quality, area_sqm,
                                  income_eur, dominical_income_eur, agricultural_income_eur, registered_office_comune_id)
SELECT p.id, rm.id, p.result_id, nullif(p.property_type, ''), um.new_id, p.subject_id, nullif(btrim(p.address), ''),
       nullif(btrim(p.partita), ''), pg_temp.norm_category(p.category), pg_temp.norm_class(p.cadastral_class),
       pg_temp.it_num(p.consistency), pg_temp.consistency_unit(p.consistency), nullif(btrim(p.census_zone), ''),
       nullif(upper(btrim(p.quality)), ''), pg_temp.it_num(p.area),
       CASE WHEN p.income !~ 'R\.D\.' THEN pg_temp.it_num(p.income) END,
       coalesce(pg_temp.it_num(p.dominical_income), pg_temp.it_num(substring(p.income from 'R\.D\.\s*Euro:\s*([0-9.,]+)'))),
       coalesce(pg_temp.it_num(p.agricultural_income), pg_temp.it_num(substring(p.income from 'R\.A\.\s*Euro:\s*([0-9.,]+)'))),
       pg_temp.comune_id(office[1], office[2])
FROM visura_properties p
JOIN req_map rm ON rm.request_ref = p.response_id
LEFT JOIN unit_map um ON um.old_id = p.location_id
LEFT JOIN LATERAL (SELECT regexp_match(p.registered_office, '^\s*(.*?)\s*\(([A-Za-z]{2})\)\s*$') AS office) o ON true;

INSERT INTO sister_next.result_owner (id, request_id, result_id, owner_index, subject_id, right_id)
SELECT DISTINCT ON (rm.id, o.result_id, o.subject_id, orm.new_id) o.id, rm.id, o.result_id, o.owner_index, o.subject_id, orm.new_id
FROM visura_owners o
JOIN req_map rm ON rm.request_ref = o.response_id
LEFT JOIN or_map orm ON orm.old_id = o.right_id
ORDER BY rm.id, o.result_id, o.subject_id, orm.new_id, o.id;

-- ---------------------------------------------------------------------------------------------------------
-- documents (file row + XML header in one row)
-- ---------------------------------------------------------------------------------------------------------
INSERT INTO sister_next.document (id, request_id, document_type, file_format, file_name, file_size, created_at, subject_text, requested_at,
                                  cadastral_unit_id, land_registry_office_id, view_subtype, protocol, protocol_year, title, reference_date,
                                  generated_at, registry_view_type, service_type, requester, liquidation_protocol, liquidation_year,
                                  liquidation_units, historical_total_area_sqm, historical_excluded_area_sqm)
SELECT d.id, rm.id, nullif(btrim(d.document_type), ''), nullif(btrim(d.file_format), ''), d.filename, d.file_size, d.created_at,
       nullif(btrim(d.subject), ''), pg_temp.it_ts(d.requested_at), um.new_id, o.id,
       nullif(btrim(m.view_subtype), ''), nullif(btrim(m.protocol), ''),
       CASE WHEN btrim(m.year) ~ '^\d{4}$' THEN btrim(m.year)::smallint END, nullif(btrim(m.title), ''),
       pg_temp.it_date(m.reference_date), pg_temp.compact_ts(m.generation_date, m.generation_time),
       nullif(btrim(m.registry_view_type), ''), nullif(btrim(m.service_type), ''), nullif(btrim(m.requester), ''),
       nullif(btrim(m.liquidation_protocol), ''), CASE WHEN btrim(m.liquidation_year) ~ '^\d{4}$' THEN btrim(m.liquidation_year)::smallint END,
       m.liquidation_units, m.historical_total_area, m.historical_excluded_area
FROM visura_documents d
LEFT JOIN req_map rm ON rm.request_ref = d.response_id
LEFT JOIN document_metadata m ON m.id = d.id
LEFT JOIN unit_map um ON um.old_id = m.location_id
LEFT JOIN sister_next.land_registry_office o ON o.name = btrim(m.source_system);

INSERT INTO sister_archive.document_content (document_id, content) SELECT id, content FROM document_metadata WHERE content IS NOT NULL;
INSERT INTO sister_next.document_subject (document_id, subject_id) SELECT id, subject_id FROM document_subjects WHERE subject_id IS NOT NULL;
INSERT INTO sister_next.document_address (id, document_id, address_text, situation)
SELECT id, document_id, btrim(address_text), nullif(btrim(situation), '') FROM building_addresses WHERE nullif(btrim(address_text), '') IS NOT NULL;

-- ---------------------------------------------------------------------------------------------------------
-- visura content: groups, buildings, land, mutations
-- ---------------------------------------------------------------------------------------------------------
INSERT INTO sister_next.property_group (id, document_id, group_index, cadastre_type, comune_id, description)
SELECT id, document_id, CASE WHEN btrim(group_index) ~ '^\d+$' THEN btrim(group_index)::int END, nullif(btrim(cadastre_type), ''),
       coalesce(pg_temp.comune_by_code(municipality_code),
                (SELECT cu.comune_id FROM building_units u JOIN building_identifiers bi ON bi.building_unit_id = u.id
                 JOIN unit_map um ON um.old_id = bi.location_id JOIN sister_next.cadastral_unit cu ON cu.id = um.new_id
                 WHERE u.group_id = property_groups.id AND cu.comune_id IS NOT NULL LIMIT 1)),
       nullif(btrim(description), '')
FROM property_groups;

INSERT INTO sister_next.building_unit (id, document_id, property_group_id, unit_index, is_current, address_text, cadastral_unit_id, sequence_id,
                                       census_zone, category, cadastral_class, cadastral_income_eur, consistency_value, consistency_unit,
                                       total_area_sqm, excluded_area_sqm, planimetria_filed_on, planimetria_protocol)
SELECT u.id, g.document_id, u.group_id, CASE WHEN btrim(u.unit_index) ~ '^\d+$' THEN btrim(u.unit_index)::int END, false, NULL,
       um.new_id, bi.sequence_id, nullif(btrim(bc.census_zone), ''), pg_temp.norm_category(bc.category),
       pg_temp.norm_class(bc.cadastral_class), bc.cadastral_income, bc.consistency_value, nullif(btrim(bc.consistency_unit), ''),
       bs.total_area, bs.excluded_area,
       pg_temp.it_date(substring(bs.planimetria from 'presentazione\s+(\d{2}/\d{2}/\d{4})')),
       substring(bs.planimetria from 'prot\.\s*n\.\s*(\S+)')
FROM building_units u
JOIN property_groups g ON g.id = u.group_id
LEFT JOIN building_classifications bc ON bc.building_unit_id = u.id
LEFT JOIN building_identifiers bi ON bi.building_unit_id = u.id
LEFT JOIN building_surfaces bs ON bs.building_unit_id = u.id
LEFT JOIN unit_map um ON um.old_id = bi.location_id;

INSERT INTO sister_next.building_unit (id, document_id, property_group_id, unit_index, is_current, address_text, cadastral_unit_id, sequence_id,
                                       census_zone, category, cadastral_class, cadastral_income_eur, consistency_value, consistency_unit,
                                       total_area_sqm, excluded_area_sqm, planimetria_filed_on, planimetria_protocol)
SELECT (SELECT coalesce(max(id), 0) FROM sister_next.building_unit) + row_number() OVER (ORDER BY s.id),
       s.id, NULL, NULL, true, nullif(btrim(s.address_text), ''), um.new_id, bi.sequence_id,
       nullif(btrim(bc.census_zone), ''), pg_temp.norm_category(bc.category), pg_temp.norm_class(bc.cadastral_class),
       bc.cadastral_income, bc.consistency_value, nullif(btrim(bc.consistency_unit), ''), bs.total_area, bs.excluded_area,
       pg_temp.it_date(substring(bs.planimetria from 'presentazione\s+(\d{2}/\d{2}/\d{4})')),
       substring(bs.planimetria from 'prot\.\s*n\.\s*(\S+)')
FROM building_current_states s
LEFT JOIN building_classifications bc ON bc.current_state_id = s.id
LEFT JOIN building_identifiers bi ON bi.current_state_id = s.id
LEFT JOIN building_surfaces bs ON bs.current_state_id = s.id
LEFT JOIN unit_map um ON um.old_id = bi.location_id;

INSERT INTO sister_next.building_unit_related_parcel (building_unit_id, cadastral_unit_id, sequence_id)
SELECT DISTINCT coalesce(r.building_unit_id, cur.id), um.new_id, r.sequence_id
FROM related_parcels r
JOIN unit_map um ON um.old_id = r.location_id
LEFT JOIN sister_next.building_unit cur ON cur.is_current AND cur.document_id = r.current_state_id
WHERE coalesce(r.building_unit_id, cur.id) IS NOT NULL;

INSERT INTO sister_next.land_parcel (id, document_id, cadastral_unit_id, partita, sequence_id)
SELECT p.id, p.document_id, um.new_id, nullif(btrim(p.partita), ''), p.sequence_id
FROM land_parcels p JOIN unit_map um ON um.old_id = p.location_id;

INSERT INTO sister_next.land_parcel_classification (id, land_parcel_id, land_quality, cadastral_class, area_sqm, deduction_symbol,
                                                    dominical_income_eur, agricultural_income_eur, dominical_income_lire, agricultural_income_lire)
SELECT c.id, c.parcel_id, nullif(upper(btrim(c.quality)), ''), pg_temp.norm_class(c.cadastral_class), c.area,
       nullif(btrim(c.deduction_symbol), ''), c.dominical_income, c.agricultural_income,
       nullif(replace(btrim(c.dominical_income_lire), '.', ''), '')::numeric, nullif(replace(btrim(c.agricultural_income_lire), '.', ''), '')::numeric
FROM land_classifications c;

INSERT INTO sister_next.ownership_mutation (id, document_id, property_group_id, land_parcel_id, mutation_index, is_current,
                                            mutation_date, source_description, status, reference_cadastral_unit_id)
SELECT m.id, coalesce(m.document_id, g.document_id, lp.document_id), m.property_group_id, m.land_parcel_id,
       CASE WHEN btrim(m.mutation_index) ~ '^\d+$' THEN btrim(m.mutation_index)::int END, m.mutation_index = 'current', pg_temp.it_date(m.mutation_date),
       nullif(btrim(m.source_description), ''), nullif(btrim(m.ref_property_status), ''), um.new_id
FROM ownership_mutations m
LEFT JOIN property_groups g ON g.id = m.property_group_id
LEFT JOIN land_parcels lp ON lp.id = m.land_parcel_id
LEFT JOIN unit_map um ON um.old_id = m.reference_location_id;

INSERT INTO sister_next.mutation_owner (id, mutation_id, position, subject_id, right_id)
SELECT DISTINCT ON (o.mutation_id, o.subject_id, orm.new_id) o.id, o.mutation_id,
       CASE WHEN btrim(o.owner_index) ~ '^\d+$' THEN btrim(o.owner_index)::int END, o.subject_id, orm.new_id
FROM property_owners o LEFT JOIN or_map orm ON orm.old_id = o.right_id
ORDER BY o.mutation_id, o.subject_id, orm.new_id, o.id;

-- ---------------------------------------------------------------------------------------------------------
-- Browser / audit trail
-- ---------------------------------------------------------------------------------------------------------
INSERT INTO sister_next.page_visit (id, request_id, step, url, screenshot_url, visited_at)
SELECT v.id, rm.id, v.step, v.url, v.screenshot_url, v.timestamp FROM page_visits v JOIN req_map rm ON rm.request_ref = v.response_id;
INSERT INTO sister_next.page_visit_form_element (id, page_visit_id, element_index, tag, element_type, name, label, value)
SELECT id, page_visit_id, element_index, tag, nullif(element_type, ''), name, label, value FROM page_visit_form_elements;
INSERT INTO sister_next.page_visit_error (id, page_visit_id, error_index, message) SELECT id, page_visit_id, error_index, message FROM page_visit_errors;

INSERT INTO sister_next.activity_log (id, started_at, finished_at, duration_ms, actor, source, action, status, target, error, client_ip)
SELECT id, started_at, finished_at, duration_ms, actor, source, action, status, target, error, client_ip FROM activity_log;
INSERT INTO sister_archive.activity_log_payload (activity_id, params, result) SELECT id, params, result FROM activity_log;

INSERT INTO sister_next.activity_log_attribute (activity_id, section, path, value)
WITH RECURSIVE walk (activity_id, section, path, val) AS (
    SELECT id, 'params', ''::text, params FROM activity_log WHERE params IS NOT NULL
    UNION ALL SELECT id, 'result', '', result FROM activity_log WHERE result IS NOT NULL
    UNION ALL
    SELECT w.activity_id, w.section, CASE WHEN w.path = '' THEN e.k ELSE w.path || '.' || e.k END, e.v
    FROM walk w
    CROSS JOIN LATERAL (
        SELECT key AS k, value AS v FROM jsonb_each(w.val) WHERE jsonb_typeof(w.val) = 'object'
        UNION ALL
        SELECT (ord - 1)::text, value FROM jsonb_array_elements(w.val) WITH ORDINALITY AS t (value, ord) WHERE jsonb_typeof(w.val) = 'array'
    ) e
)
SELECT activity_id, section, coalesce(nullif(path, ''), '$'),
       CASE WHEN jsonb_typeof(val) = 'null' THEN NULL ELSE val #>> '{}' END
FROM walk
WHERE jsonb_typeof(val) NOT IN ('object', 'array') OR val IN ('{}'::jsonb, '[]'::jsonb);

-- ---------------------------------------------------------------------------------------------------------
-- Structured extractions and mortgage inspections
-- ---------------------------------------------------------------------------------------------------------
INSERT INTO sister_next.document_extraction (id, document_id, provider, model, schema_name, schema_version, run_id, workflow_id,
                                             output_path, extracted_at, created_at)
SELECT id, document_id, provider, model, schema_name, schema_version, run_id, workflow_id, output_path, extracted_at, created_at
FROM document_structured_extractions;
INSERT INTO sister_archive.document_extraction_payload (extraction_id, structured_data)
SELECT id, structured_data FROM document_structured_extractions;

INSERT INTO sister_next.mortgage_inspection
SELECT i.extraction_id, pg_temp.it_date(i.inspection_date), nullif(i.inspection_time, '')::time, nullif(i.inspection_number, ''),
       pg_temp.it_date(i.inspection_date_number), nullif(i.inspection_start, '')::timestamp, nullif(i.requester, ''), i.tax_paid_euro,
       i.page_count, nullif(i.office, ''), nullif(i.service, ''), nullif(i.note_type, ''), nullif(i.note_timestamp, '')::timestamptz,
       i.registro_generale, i.registro_particolare, i.presentazione_numero, pg_temp.it_date(i.presentazione_data),
       i.section_a_other_data, i.section_d_text,
       nullif(l.lien_type, ''), l.derived_from, l.derived_from_code, l.derived_from_description, l.principal_euro,
       l.annual_interest_rate_pct::numeric(7, 4), l.total_euro, l.variable_amounts, l.automatic_increase, l.resolutive_condition,
       l.duration_years, l.duration_description, l.mortgage_rank,
       nullif(t.title_type, ''), t.description, pg_temp.it_date(t.title_date), t.repertory_number, t.notary_name, t.notary_fiscal_code,
       t.notary_location,
       l.semiannual_interest_rate, l.interest_euro, l.expenses_euro, l.foreign_currency
FROM mortgage_inspections i
LEFT JOIN mortgage_inspection_liens l ON l.extraction_id = i.extraction_id
LEFT JOIN mortgage_inspection_titles t ON t.extraction_id = i.extraction_id;

-- Make sure sequences are past the preserved ids before allocating subject ids for the parties.
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT format('%I.%I', n.nspname, c.relname) AS tbl, a.attname AS col
        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('sister_next', 'sister_archive') AND a.attidentity <> '' AND NOT a.attisdropped
    LOOP
        EXECUTE format('SELECT setval(pg_get_serial_sequence(%L, %L), greatest(coalesce((SELECT max(%I) FROM %s), 0), 1), (SELECT max(%I) FROM %s) IS NOT NULL)',
                       r.tbl, r.col, r.col, r.tbl, r.col, r.tbl);
    END LOOP;
END $$;

-- Parties become subjects: matched on fiscal code when there is one, otherwise one subject per name + birth date.
CREATE TEMP TABLE _party ON COMMIT DROP AS
SELECT p.*, nullif(upper(regexp_replace(p.fiscal_code, '\s', '', 'g')), '') AS cf,
       coalesce(nullif(btrim(p.entity_name), ''), nullif(btrim(concat_ws(' ', p.surname, p.given_name)), ''), '(sconosciuto)') AS full_name,
       pg_temp.it_date(p.birth_date) AS dob
FROM mortgage_inspection_parties p;
CREATE TEMP TABLE _party_key ON COMMIT DROP AS
SELECT k.key, coalesce(s.id, nextval(pg_get_serial_sequence('sister_next.subject', 'id'))) AS subject_id, s.id IS NULL AS is_new
FROM (SELECT DISTINCT coalesce(cf, 'N:' || full_name || ':' || coalesce(dob::text, '')) AS key, cf FROM _party) k
LEFT JOIN sister_next.subject s ON s.fiscal_code = k.cf;
INSERT INTO sister_next.subject (id, subject_type, fiscal_code, name, last_name, first_name, gender, birth_date, birth_comune_id)
SELECT DISTINCT ON (pk.subject_id) pk.subject_id,
       CASE WHEN nullif(btrim(p.entity_name), '') IS NOT NULL THEN 'legal_entity' ELSE 'person' END, p.cf, p.full_name,
       nullif(btrim(p.surname), ''), nullif(btrim(p.given_name), ''), CASE WHEN p.gender IN ('M', 'F') THEN p.gender END, p.dob,
       pg_temp.comune_id(b[1], b[2])
FROM _party p
JOIN _party_key pk ON pk.key = coalesce(p.cf, 'N:' || p.full_name || ':' || coalesce(p.dob::text, '')) AND pk.is_new
LEFT JOIN LATERAL (SELECT regexp_match(p.birth_place, '^\s*(.*?)\s*\(([A-Za-z]{2})\)\s*$') AS b) bp ON true
ORDER BY pk.subject_id, p.id;

INSERT INTO sister_next.mortgage_inspection_party (id, inspection_id, role, ordinal, subject_id, subject_number, quality, registered_office,
                                                   mortgage_domicile, negotiation_unit_reference, right_type, share_numerator, share_denominator)
SELECT p.id, p.extraction_id, p.role, p.ordinal, pk.subject_id, p.subject_number, nullif(p.quality, ''), nullif(p.registered_office, ''),
       nullif(p.mortgage_domicile, ''), p.negotiation_unit_reference, nullif(p.right_type, ''), p.share_numerator, p.share_denominator
FROM _party p JOIN _party_key pk ON pk.key = coalesce(p.cf, 'N:' || p.full_name || ':' || coalesce(p.dob::text, ''));

-- Mortgage properties point at cadastral units; create the ones we did not know yet.
CREATE TEMP TABLE _mp ON COMMIT DROP AS
SELECT mp.*, u.extraction_id, u.unit_number,
       coalesce(pg_temp.comune_by_code(mp.municipality_code),
                pg_temp.comune_id(coalesce(mm.m[1], mp.municipality), mm.m[2])) AS cid,
       CASE lower(btrim(mp.cadastre_type)) WHEN 'fabbricati' THEN 'F' WHEN 'terreni' THEN 'T' END AS ct,
       nullif(btrim(mp.urban_section), '') AS sec,
       mp.sheet::text AS sh, mp.parcel::text AS pa, mp.subunit::text AS su
FROM mortgage_inspection_properties mp
JOIN mortgage_inspection_units u ON u.id = mp.unit_id
CROSS JOIN LATERAL (SELECT regexp_match(mp.municipality, '^\s*(.*?)\s*\(([A-Za-z]{2})\)\s*$') AS m) mm;
INSERT INTO sister_next.cadastral_unit (cadastre_type, comune_id, section, sheet, parcel, subunit)
SELECT DISTINCT ct, cid, sec, sh, pa, su FROM _mp
ON CONFLICT ON CONSTRAINT uq_cadastral_unit DO NOTHING;
INSERT INTO sister_next.mortgage_inspection_property (id, inspection_id, unit_number, property_number, cadastral_unit_id, nature,
                                                      nature_description, room_count, floor, address, civic_number, area_sqm)
SELECT m.id, m.extraction_id, m.unit_number, m.property_number, cu.id, nullif(m.nature, ''), nullif(m.nature_description, ''),
       m.room_count, nullif(m.floor, ''), nullif(m.address, ''), nullif(m.civic_number, ''), m.area_sqm
FROM _mp m
LEFT JOIN sister_next.cadastral_unit cu ON cu.cadastre_type IS NOT DISTINCT FROM m.ct AND cu.comune_id IS NOT DISTINCT FROM m.cid
     AND cu.section IS NOT DISTINCT FROM m.sec AND cu.sheet IS NOT DISTINCT FROM m.sh AND cu.parcel IS NOT DISTINCT FROM m.pa
     AND cu.subunit IS NOT DISTINCT FROM m.su;

-- ---------------------------------------------------------------------------------------------------------
-- Geometry cache and the raw XML tree (archive)
-- ---------------------------------------------------------------------------------------------------------
INSERT INTO sister_next.parcel_geometry_cache (feature_type, comune_id, section, sheet, parcel, source_reference, geom, checked_at)
SELECT DISTINCT ON (g.feature_type, cid, g.section, g.sheet, g.parcel)
       g.feature_type, cid, g.section, g.sheet, g.parcel, g.source_reference, g.geom, g.checked_at
FROM cadastral_map_geometry_cache g
CROSS JOIN LATERAL (SELECT coalesce(pg_temp.comune_by_code(g.municipality_code), pg_temp.comune_id(g.municipality, g.province)) AS cid) c
ORDER BY g.feature_type, cid, g.section, g.sheet, g.parcel, g.checked_at DESC;

INSERT INTO sister_archive.document_xml_node (id, document_id, parent_id, ordinal, tag, text)
SELECT id, document_id, parent_id, ordinal, tag, text FROM document_xml_nodes;
INSERT INTO sister_archive.document_xml_attribute (id, node_id, name, value) SELECT id, node_id, name, value FROM document_xml_attributes;

-- ---------------------------------------------------------------------------------------------------------
-- Source values that were not typed
-- ---------------------------------------------------------------------------------------------------------
INSERT INTO sister_archive.migration_unparsed (target, source_id, source_value)
SELECT 'subject.birth_date', s.id, s.date_of_birth FROM cadastral_subjects s
JOIN sister_next.subject n ON n.id = s.id WHERE nullif(btrim(s.date_of_birth), '') IS NOT NULL AND n.birth_date IS NULL
UNION ALL SELECT 'ownership_right.share', r.id, r.ownership_share FROM ownership_rights r
JOIN _or o ON o.id = r.id WHERE nullif(btrim(r.ownership_share), '') IS NOT NULL AND o.num IS NULL
UNION ALL SELECT 'ownership_right.start_date', r.id, r.start_date FROM ownership_rights r
JOIN _or o ON o.id = r.id WHERE nullif(btrim(r.start_date), '') IS NOT NULL AND o.sd IS NULL
UNION ALL SELECT 'ownership_right.end_date', r.id, r.end_date FROM ownership_rights r
JOIN _or o ON o.id = r.id WHERE nullif(btrim(r.end_date), '') IS NOT NULL AND o.ed IS NULL
UNION ALL SELECT 'property.consistency', p.id, p.consistency FROM visura_properties p
JOIN sister_next.property n ON n.id = p.id WHERE nullif(btrim(p.consistency), '') IS NOT NULL AND n.consistency_value IS NULL
UNION ALL SELECT 'property.income', p.id, p.income FROM visura_properties p
JOIN sister_next.property n ON n.id = p.id
WHERE p.income ~ '[0-9]' AND n.income_eur IS NULL AND n.dominical_income_eur IS NULL AND n.agricultural_income_eur IS NULL
UNION ALL SELECT 'property.area', p.id, p.area FROM visura_properties p
JOIN sister_next.property n ON n.id = p.id WHERE nullif(btrim(p.area), '') IS NOT NULL AND n.area_sqm IS NULL
UNION ALL SELECT 'property.category', p.id, p.category FROM visura_properties p
JOIN sister_next.property n ON n.id = p.id WHERE p.category ~ '[A-Za-z0-9]' AND n.category IS NULL
UNION ALL SELECT 'property.registered_office', p.id, p.registered_office FROM visura_properties p
JOIN sister_next.property n ON n.id = p.id WHERE nullif(btrim(p.registered_office), '') IS NOT NULL AND n.registered_office_comune_id IS NULL
UNION ALL SELECT 'document.requested_at', d.id, d.requested_at FROM visura_documents d
JOIN sister_next.document n ON n.id = d.id WHERE nullif(btrim(d.requested_at), '') IS NOT NULL AND n.requested_at IS NULL
UNION ALL SELECT 'document.reference_date', m.id, m.reference_date FROM document_metadata m
JOIN sister_next.document n ON n.id = m.id WHERE nullif(btrim(m.reference_date), '') IS NOT NULL AND n.reference_date IS NULL
UNION ALL SELECT 'document.generated_at', m.id, m.generation_date || ' ' || m.generation_time FROM document_metadata m
JOIN sister_next.document n ON n.id = m.id WHERE nullif(btrim(m.generation_date), '') IS NOT NULL AND n.generated_at IS NULL
UNION ALL SELECT 'building_unit.planimetria', s.id, s.planimetria FROM building_surfaces s
WHERE nullif(btrim(s.planimetria), '') IS NOT NULL AND s.planimetria !~ 'presentazione\s+\d{2}/\d{2}/\d{4}'
UNION ALL SELECT 'comune.cadastral_code', NULL, name || ' (' || coalesce(prov, '-') || ') codes: ' || string_agg(DISTINCT code, ',')
FROM _c_norm WHERE code IS NOT NULL GROUP BY name, prov HAVING count(DISTINCT code) > 1
UNION ALL SELECT 'property_group.comune_code', g.id, g.municipality_code FROM property_groups g
JOIN sister_next.property_group n ON n.id = g.id WHERE nullif(btrim(g.municipality_code), '') IS NOT NULL AND n.comune_id IS NULL;

-- leave every identity sequence past the highest id again (rows were added after the first pass)
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT format('%I.%I', n.nspname, c.relname) AS tbl, a.attname AS col
        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('sister_next', 'sister_archive') AND a.attidentity <> '' AND NOT a.attisdropped
    LOOP
        EXECUTE format('SELECT setval(pg_get_serial_sequence(%L, %L), greatest(coalesce((SELECT max(%I) FROM %s), 0), 1), (SELECT max(%I) FROM %s) IS NOT NULL)',
                       r.tbl, r.col, r.col, r.tbl, r.col, r.tbl);
    END LOOP;
END $$;
