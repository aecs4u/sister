-- The canonical place keys are istat.municipalities.numeric_code. Names are rendered in the legacy uppercase form.
CREATE VIEW __SISTER_SCHEMA__.v_cadastral_unit AS
SELECT u.id AS cadastral_unit_id,
       u.cadastre_type,
       m.numeric_code AS comune_id,
       upper(coalesce(m.italian_name, m.name)) AS comune,
       m.province_abbreviation AS province_code,
       m.cadastral_code AS comune_code,
       u.section,
       u.sheet,
       u.parcel,
       u.subunit,
       CASE WHEN u.sheet IS NULL OR u.parcel IS NULL THEN 'incomplete'
            WHEN upper(u.parcel) ~ '^[A-Z0-9]{16}$' THEN 'possible_fiscal_code'
            ELSE 'cadastral_parcel' END AS parcel_key_status
FROM __SISTER_SCHEMA__.cadastral_unit u
LEFT JOIN __ISTAT_SCHEMA__.municipalities m ON m.numeric_code = u.comune_id;

CREATE VIEW __SISTER_SCHEMA__.v_property AS
SELECT p.id AS property_id,
       p.request_id,
       r.request_ref,
       r.request_type,
       r.created_at AS request_created_at,
       r.success AS request_success,
       r.responded_at,
       p.result_id,
       cu.cadastral_unit_id,
       cu.cadastre_type,
       cu.comune,
       cu.province_code,
       cu.comune_code,
       cu.section,
       cu.sheet,
       cu.parcel,
       cu.subunit,
       cu.parcel_key_status,
       p.property_type,
       p.address,
       p.partita,
       p.category,
       p.cadastral_class,
       p.consistency_value,
       p.consistency_unit,
       p.income_eur,
       p.dominical_income_eur,
       p.agricultural_income_eur,
       p.census_zone,
       p.land_quality,
       p.area_sqm,
       upper(coalesce(office.italian_name, office.name)) AS registered_office_comune,
       office.province_abbreviation AS registered_office_province,
       p.subject_id,
       s.fiscal_code,
       s.name AS subject_name
FROM __SISTER_SCHEMA__.property p
JOIN __SISTER_SCHEMA__.request r ON r.id = p.request_id
LEFT JOIN __SISTER_SCHEMA__.v_cadastral_unit cu ON cu.cadastral_unit_id = p.cadastral_unit_id
LEFT JOIN __SISTER_SCHEMA__.subject s ON s.id = p.subject_id
LEFT JOIN __ISTAT_SCHEMA__.municipalities office ON office.numeric_code = p.registered_office_comune_id;

CREATE VIEW __SISTER_SCHEMA__.v_result_owner AS
SELECT o.id AS result_owner_id,
       o.request_id,
       r.request_ref,
       r.request_type,
       r.created_at AS request_created_at,
       r.success AS request_success,
       o.result_id,
       rr.result_index,
       rr.visura_present,
       o.owner_index,
       s.id AS subject_id,
       s.fiscal_code,
       s.name AS subject_name,
       s.last_name,
       s.first_name,
       s.gender,
       s.subject_type,
       s.birth_date,
       coalesce(upper(birthplace.italian_name), upper(birthplace.name), s.birth_municipality_text) AS birth_comune,
       coalesce(birthplace.province_abbreviation, s.birth_province_code) AS birth_province,
       o.right_id,
       rk.right_type,
       rk.right_code,
       rk.description AS right_description,
       orr.share_numerator,
       orr.share_denominator,
       orr.start_date AS right_start_date,
       orr.end_date AS right_end_date
FROM __SISTER_SCHEMA__.result_owner o
JOIN __SISTER_SCHEMA__.request r ON r.id = o.request_id
LEFT JOIN __SISTER_SCHEMA__.request_result rr ON rr.id = o.result_id
LEFT JOIN __SISTER_SCHEMA__.subject s ON s.id = o.subject_id
LEFT JOIN __ISTAT_SCHEMA__.municipalities birthplace ON birthplace.numeric_code = s.birth_comune_id
LEFT JOIN __SISTER_SCHEMA__.ownership_right orr ON orr.id = o.right_id
LEFT JOIN __SISTER_SCHEMA__.right_kind rk ON rk.id = orr.right_kind_id;

CREATE VIEW __SISTER_SCHEMA__.v_property_owner AS
SELECT l.property_id, l.link_scope, o.*
FROM (
    SELECT p.id AS property_id, o.id AS result_owner_id, 'result'::text AS link_scope
    FROM __SISTER_SCHEMA__.property p JOIN __SISTER_SCHEMA__.result_owner o ON o.result_id = p.result_id
    UNION ALL
    SELECT p.id, o.id, 'request'
    FROM __SISTER_SCHEMA__.property p JOIN __SISTER_SCHEMA__.result_owner o ON o.request_id = p.request_id
    WHERE p.result_id IS NULL AND o.result_id IS NULL
    UNION ALL
    SELECT p.id, o.id, 'subject'
    FROM __SISTER_SCHEMA__.property p JOIN __SISTER_SCHEMA__.result_owner o ON o.subject_id = p.subject_id
) l
JOIN __SISTER_SCHEMA__.v_result_owner o ON o.result_owner_id = l.result_owner_id;

CREATE VIEW __SISTER_SCHEMA__.v_document_cadastral_unit AS
SELECT x.document_id,
       x.source,
       d.request_id,
       d.document_type,
       d.file_format,
       d.file_name,
       d.created_at,
       d.requested_at,
       d.view_subtype,
       d.protocol,
       d.reference_date,
       cu.*
FROM (
    SELECT id AS document_id, cadastral_unit_id, 'metadata'::text AS source
    FROM __SISTER_SCHEMA__.document WHERE cadastral_unit_id IS NOT NULL
    UNION
    SELECT document_id, cadastral_unit_id, 'land_parcel' FROM __SISTER_SCHEMA__.land_parcel
    UNION
    SELECT document_id, cadastral_unit_id, 'building_unit'
    FROM __SISTER_SCHEMA__.building_unit WHERE cadastral_unit_id IS NOT NULL
) x
JOIN __SISTER_SCHEMA__.document d ON d.id = x.document_id
JOIN __SISTER_SCHEMA__.v_cadastral_unit cu ON cu.cadastral_unit_id = x.cadastral_unit_id;

CREATE VIEW __SISTER_SCHEMA__.v_cadastral_unit_index AS
SELECT cu.*,
       coalesce(pc.n, 0) AS property_count,
       coalesce(rq.n, 0) AS request_count,
       coalesce(dc.n, 0) AS document_count
FROM __SISTER_SCHEMA__.v_cadastral_unit cu
LEFT JOIN (SELECT cadastral_unit_id, count(*) AS n FROM __SISTER_SCHEMA__.property GROUP BY 1) pc
       ON pc.cadastral_unit_id = cu.cadastral_unit_id
LEFT JOIN (SELECT cadastral_unit_id, count(*) AS n FROM __SISTER_SCHEMA__.request GROUP BY 1) rq
       ON rq.cadastral_unit_id = cu.cadastral_unit_id
LEFT JOIN (
    SELECT cadastral_unit_id, count(DISTINCT document_id) AS n
    FROM __SISTER_SCHEMA__.v_document_cadastral_unit GROUP BY 1
) dc ON dc.cadastral_unit_id = cu.cadastral_unit_id
WHERE pc.n > 0 OR rq.n > 0 OR dc.n > 0;

CREATE VIEW __SISTER_SCHEMA__.v_visura_cadastral_polygon AS
WITH located AS (
    SELECT DISTINCT v.document_id, v.comune_id, v.comune_code, v.comune, v.province_code,
           coalesce(v.section, '') AS section, v.sheet, v.parcel
    FROM __SISTER_SCHEMA__.v_document_cadastral_unit v
    WHERE v.document_type LIKE 'visura%' AND v.comune_id IS NOT NULL AND v.province_code IS NOT NULL
      AND v.sheet IS NOT NULL AND v.parcel IS NOT NULL
), features AS (
    SELECT document_id, 'parcel'::text AS feature_type, comune_id, comune_code, comune, province_code,
           section, sheet, parcel FROM located
    UNION
    SELECT document_id, 'sheet', comune_id, comune_code, comune, province_code, section, sheet, '' FROM located
)
SELECT f.document_id,
       f.feature_type,
       f.comune_id,
       f.comune_code,
       f.comune,
       f.province_code,
       f.section,
       f.sheet,
       f.parcel,
       g.source_reference,
       g.checked_at,
       g.geom
FROM features f
LEFT JOIN __SISTER_SCHEMA__.parcel_geometry_cache g
       ON g.feature_type = f.feature_type AND g.comune_id = f.comune_id AND g.section = f.section
      AND g.sheet = f.sheet AND g.parcel = f.parcel;
