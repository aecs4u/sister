-- Durable, indexed distributions used by the OMI zone valuation page.
-- Run against the PostgreSQL database whose normalized source tables are in schema omi.
-- Re-run CALL omi.refresh_zone_quote_benchmarks() after each normalized OMI data import.

CREATE TABLE IF NOT EXISTS omi.zone_quote_benchmark_sample (
    period_year smallint NOT NULL,
    period_semester smallint NOT NULL CHECK (period_semester IN (1, 2)),
    property_type_code integer NOT NULL,
    conservation_status_code text NOT NULL,
    market_band text NOT NULL,
    measure text NOT NULL CHECK (measure IN ('sale', 'rent')),
    surface_class_code text NOT NULL,
    scope_code text NOT NULL CHECK (scope_code IN ('comune', 'provincia', 'regione', 'italia')),
    scope_id bigint NOT NULL,
    sample_id bigint NOT NULL,
    sample_value double precision NOT NULL,
    refreshed_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (
        period_year, period_semester, property_type_code, conservation_status_code,
        market_band, measure, surface_class_code, scope_code, scope_id, sample_id
    )
);

CREATE TABLE IF NOT EXISTS omi.zone_quote_benchmark_summary (
    period_year smallint NOT NULL,
    period_semester smallint NOT NULL CHECK (period_semester IN (1, 2)),
    property_type_code integer NOT NULL,
    conservation_status_code text NOT NULL,
    market_band text NOT NULL,
    measure text NOT NULL CHECK (measure IN ('sale', 'rent')),
    surface_class_code text NOT NULL,
    scope_code text NOT NULL CHECK (scope_code IN ('comune', 'provincia', 'regione', 'italia')),
    scope_id bigint NOT NULL,
    sample_unit text NOT NULL CHECK (sample_unit IN ('zone', 'comuni')),
    sample_count integer NOT NULL,
    mean_value double precision NOT NULL,
    median_value double precision NOT NULL,
    q1_value double precision NOT NULL,
    q3_value double precision NOT NULL,
    refreshed_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (
        period_year, period_semester, property_type_code, conservation_status_code,
        market_band, measure, surface_class_code, scope_code, scope_id
    )
);

COMMENT ON TABLE omi.zone_quote_benchmark_sample IS
    'Persistent zone/comune benchmark values used to compute OMI valuation percentiles.';
COMMENT ON TABLE omi.zone_quote_benchmark_summary IS
    'Persistent OMI benchmark mean, median and quartiles by period, type, condition, band, surface and geographic scope.';

CREATE INDEX IF NOT EXISTS ix_omi_zone_quote_benchmark_sample_lookup
    ON omi.zone_quote_benchmark_sample (
        period_year, period_semester, property_type_code, conservation_status_code,
        market_band, measure, surface_class_code, scope_code, scope_id, sample_value
    ) INCLUDE (sample_id);

GRANT SELECT ON omi.zone_quote_benchmark_sample, omi.zone_quote_benchmark_summary TO PUBLIC;

CREATE OR REPLACE PROCEDURE omi.refresh_zone_quote_benchmarks()
LANGUAGE plpgsql
AS $refresh$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('omi.zone_quote_benchmark'));
    TRUNCATE TABLE omi.zone_quote_benchmark_sample;
    TRUNCATE TABLE omi.zone_quote_benchmark_summary;

    INSERT INTO omi.zone_quote_benchmark_sample (
        period_year, period_semester, property_type_code, conservation_status_code,
        market_band, measure, surface_class_code, scope_code, scope_id, sample_id, sample_value
    )
    WITH preferred_quotes AS MATERIALIZED (
        SELECT DISTINCT ON (
                   zm.zone_municipality_id, upper(trim(z.zone_name)), p.year, p.semester, pt.source_code,
                   upper(cs.status_code), upper(trim(z.market_band)), sp.class_code, sr.class_code
               )
               zm.zone_municipality_id,
               zm.province_id,
               zm.region_id,
               z.zone_version_id,
               COALESCE(ps.state_code = 'P', false) AS is_prevalent,
               z.zone_name,
               upper(trim(z.market_band)) AS market_band,
               p.year::smallint AS period_year,
               p.semester::smallint AS period_semester,
               pt.source_code::integer AS property_type_code,
               upper(cs.status_code) AS conservation_status_code,
               COALESCE((f.purchase_min + f.purchase_max) / 2.0, f.purchase_min, f.purchase_max)::double precision AS sale_value,
               sp.class_code AS sale_surface,
               COALESCE((f.rental_min + f.rental_max) / 2.0, f.rental_min, f.rental_max)::double precision AS rent_value,
               sr.class_code AS rent_surface
        FROM omi.zone_municipality zm
        JOIN omi.zone_version z ON z.zone_municipality_id = zm.zone_municipality_id
        JOIN omi.period p ON p.period_id = z.period_id
        JOIN omi.quotation_fact f ON f.zone_version_id = z.zone_version_id
        JOIN omi.property_type pt ON pt.property_type_id = f.property_type_id
        JOIN omi.conservation_status cs ON cs.conservation_status_id = f.conservation_status_id
        LEFT JOIN omi.prevalent_state ps ON ps.prevalent_state_id = f.prevalent_state_id
        LEFT JOIN omi.surface_class sp ON sp.surface_class_id = f.purchase_surface_id
        LEFT JOIN omi.surface_class sr ON sr.surface_class_id = f.rental_surface_id
        WHERE pt.source_code IS NOT NULL
          AND cs.status_code IS NOT NULL
          AND NULLIF(trim(z.market_band), '') IS NOT NULL
          AND (f.purchase_min IS NOT NULL OR f.purchase_max IS NOT NULL
               OR f.rental_min IS NOT NULL OR f.rental_max IS NOT NULL)
        ORDER BY zm.zone_municipality_id, upper(trim(z.zone_name)), p.year, p.semester, pt.source_code,
                 upper(cs.status_code), upper(trim(z.market_band)), sp.class_code, sr.class_code,
                 COALESCE(ps.state_code = 'P', false) DESC, z.zone_version_id DESC
    ), raw_zone_values AS MATERIALIZED (
        SELECT zone_municipality_id AS municipality_id, province_id, region_id,
               zone_version_id AS zone_id, zone_version_id, is_prevalent,
               period_year, period_semester, property_type_code, conservation_status_code,
               market_band, 'sale'::text AS measure, sale_surface AS surface_class_code,
               sale_value AS sample_value
        FROM preferred_quotes
        WHERE sale_surface IS NOT NULL AND sale_value IS NOT NULL
        UNION ALL
        SELECT zone_municipality_id, province_id, region_id, zone_version_id,
               zone_version_id, is_prevalent, period_year, period_semester,
               property_type_code, conservation_status_code, market_band,
               'rent'::text, rent_surface, rent_value
        FROM preferred_quotes
        WHERE rent_surface IS NOT NULL AND rent_value IS NOT NULL
    ), zone_values AS MATERIALIZED (
        SELECT DISTINCT ON (
                   period_year, period_semester, property_type_code, conservation_status_code,
                   market_band, measure, surface_class_code, municipality_id, zone_id
               )
               municipality_id, province_id, region_id, zone_id,
               period_year, period_semester, property_type_code, conservation_status_code,
               market_band, measure, surface_class_code, sample_value
        FROM raw_zone_values
        ORDER BY period_year, period_semester, property_type_code, conservation_status_code,
                 market_band, measure, surface_class_code, municipality_id,
                 zone_id, is_prevalent DESC, zone_version_id DESC
    ), municipality_values AS MATERIALIZED (
        SELECT period_year, period_semester, property_type_code, conservation_status_code,
               market_band, measure, surface_class_code, municipality_id, province_id, region_id,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY sample_value)::double precision AS sample_value
        FROM zone_values
        GROUP BY period_year, period_semester, property_type_code, conservation_status_code,
                 market_band, measure, surface_class_code, municipality_id, province_id, region_id
    ), scope_samples AS (
        SELECT period_year, period_semester, property_type_code, conservation_status_code,
               market_band, measure, surface_class_code, 'comune'::text AS scope_code,
               municipality_id AS scope_id, zone_id AS sample_id, sample_value, 'zone'::text AS sample_unit
        FROM zone_values
        UNION ALL
        SELECT period_year, period_semester, property_type_code, conservation_status_code,
               market_band, measure, surface_class_code, 'provincia', province_id, zone_id, sample_value, 'zone'
        FROM zone_values
        WHERE province_id IS NOT NULL
        UNION ALL
        SELECT period_year, period_semester, property_type_code, conservation_status_code,
               market_band, measure, surface_class_code, 'regione', region_id, municipality_id, sample_value, 'comuni'
        FROM municipality_values
        WHERE region_id IS NOT NULL
        UNION ALL
        SELECT period_year, period_semester, property_type_code, conservation_status_code,
               market_band, measure, surface_class_code, 'italia', 0, municipality_id, sample_value, 'comuni'
        FROM municipality_values
    )
    SELECT period_year, period_semester, property_type_code, conservation_status_code,
           market_band, measure, surface_class_code, scope_code, scope_id, sample_id, sample_value
    FROM scope_samples;

    INSERT INTO omi.zone_quote_benchmark_summary (
        period_year, period_semester, property_type_code, conservation_status_code,
        market_band, measure, surface_class_code, scope_code, scope_id, sample_unit,
        sample_count, mean_value, median_value, q1_value, q3_value
    )
    SELECT period_year, period_semester, property_type_code, conservation_status_code,
           market_band, measure, surface_class_code, scope_code, scope_id,
           min(CASE WHEN scope_code IN ('comune', 'provincia') THEN 'zone' ELSE 'comuni' END),
           count(*)::integer,
           avg(sample_value)::double precision,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY sample_value)::double precision,
           percentile_cont(0.25) WITHIN GROUP (ORDER BY sample_value)::double precision,
           percentile_cont(0.75) WITHIN GROUP (ORDER BY sample_value)::double precision
    FROM omi.zone_quote_benchmark_sample
    GROUP BY period_year, period_semester, property_type_code, conservation_status_code,
             market_band, measure, surface_class_code, scope_code, scope_id;
END;
$refresh$;

REVOKE ALL ON PROCEDURE omi.refresh_zone_quote_benchmarks() FROM PUBLIC;

CALL omi.refresh_zone_quote_benchmarks();
