-- Forward fix for databases that ran the first variant of 20261024_flat_normalised (the one applied to the live database on
-- 2026-10-23). That variant
--   * turned the 602 `mutation_index = 'current'` markers into NULL, and
--   * dropped XML header fields that are defined by the visura schema (requester, registry_view_type, service_type,
--     liquidation_units, historical areas, mutation_date) because every current row was empty or constant.
-- Everything is rebuilt from `sister_legacy` (ids were preserved), and every step is idempotent: on a database that already ran
-- the corrected 20261024 it changes nothing.
SET LOCAL search_path = sister, public;

-- Databases already stamped at 20261024 may have run the earlier revision body,
-- before it included durable request jobs. Keep fresh and upgraded schemas aligned.
CREATE TABLE IF NOT EXISTS sister.sister_request_jobs (
    request_id varchar PRIMARY KEY,
    request_type varchar NOT NULL,
    payload_json text NOT NULL,
    status varchar NOT NULL DEFAULT 'queued',
    error_kind varchar,
    attempts integer NOT NULL DEFAULT 0,
    side_effect_started boolean NOT NULL DEFAULT false,
    lease_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_sister_request_jobs_status_lease
    ON sister.sister_request_jobs (status, lease_until);

CREATE FUNCTION pg_temp.it_date(t text) RETURNS date LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF t ~ '^\s*\d{2}/\d{2}/\d{4}\s*$' THEN RETURN to_date(btrim(t), 'DD/MM/YYYY'); END IF;
    IF t ~ '^\s*\d{4}-\d{2}-\d{2}' THEN RETURN left(btrim(t), 10)::date; END IF;
    IF t ~ '^\s*\d{8}\s*$' THEN RETURN to_date(btrim(t), 'YYYYMMDD'); END IF;
    RETURN NULL;
EXCEPTION WHEN others THEN RETURN NULL;
END $$;

-- ---- columns ---------------------------------------------------------------------------------------------------------
ALTER TABLE sister.ownership_mutation ADD COLUMN IF NOT EXISTS is_current boolean NOT NULL DEFAULT false;
ALTER TABLE sister.ownership_mutation ADD COLUMN IF NOT EXISTS mutation_date date;
ALTER TABLE sister.document ADD COLUMN IF NOT EXISTS registry_view_type text;
ALTER TABLE sister.document ADD COLUMN IF NOT EXISTS service_type text;
ALTER TABLE sister.document ADD COLUMN IF NOT EXISTS requester text;
ALTER TABLE sister.document ADD COLUMN IF NOT EXISTS liquidation_units integer;
ALTER TABLE sister.document ADD COLUMN IF NOT EXISTS historical_total_area_sqm numeric;
ALTER TABLE sister.document ADD COLUMN IF NOT EXISTS historical_excluded_area_sqm numeric;

-- ---- values back from the legacy tables (only where the new column is still empty) --------------------------------------
DO $$
BEGIN
    IF to_regclass('sister_legacy.ownership_mutations') IS NULL OR to_regclass('sister_legacy.document_metadata') IS NULL THEN
        RAISE NOTICE 'sister_legacy is gone: columns added, nothing to backfill';
        RETURN;
    END IF;

    UPDATE sister.ownership_mutation m SET is_current = true
    FROM sister_legacy.ownership_mutations l
    WHERE l.id = m.id AND l.mutation_index = 'current' AND NOT m.is_current;

    UPDATE sister.ownership_mutation m SET mutation_date = pg_temp.it_date(l.mutation_date)
    FROM sister_legacy.ownership_mutations l
    WHERE l.id = m.id AND m.mutation_date IS NULL AND pg_temp.it_date(l.mutation_date) IS NOT NULL;

    UPDATE sister.document d SET
        registry_view_type = coalesce(d.registry_view_type, nullif(btrim(m.registry_view_type), '')),
        service_type = coalesce(d.service_type, nullif(btrim(m.service_type), '')),
        requester = coalesce(d.requester, nullif(btrim(m.requester), '')),
        liquidation_units = coalesce(d.liquidation_units, m.liquidation_units),
        historical_total_area_sqm = coalesce(d.historical_total_area_sqm, m.historical_total_area),
        historical_excluded_area_sqm = coalesce(d.historical_excluded_area_sqm, m.historical_excluded_area)
    FROM sister_legacy.document_metadata m
    WHERE m.id = d.id
      AND (d.registry_view_type IS NULL OR d.service_type IS NULL OR d.requester IS NULL OR d.liquidation_units IS NULL
           OR d.historical_total_area_sqm IS NULL OR d.historical_excluded_area_sqm IS NULL);
END $$;

-- ---- checks (only meaningful while sister_legacy still exists) --------------------------------------------------------------
DO $$
DECLARE expected bigint; actual bigint;
BEGIN
    IF to_regclass('sister_legacy.ownership_mutations') IS NULL THEN RETURN; END IF;
    SELECT count(*) INTO expected FROM sister_legacy.ownership_mutations WHERE mutation_index = 'current';
    SELECT count(*) INTO actual FROM sister.ownership_mutation WHERE is_current;
    IF expected <> actual THEN
        RAISE EXCEPTION 'fix check failed: current-ownership markers (expected %, got %)', expected, actual;
    END IF;
END $$;
