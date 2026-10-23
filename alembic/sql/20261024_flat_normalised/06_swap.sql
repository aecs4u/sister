-- Step 6: swap. The legacy tables and views move to `sister_legacy` (kept, untouched, for rollback and comparison);
-- the new tables and views move into `sister`. PostGIS, spatial_ref_sys and alembic_version stay where they are.
-- Moving a relation between schemas is a catalog change: it keeps data, indexes, sequences and view dependencies.
CREATE SCHEMA sister_legacy;

DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT c.relname, c.relkind
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'sister' AND c.relkind IN ('r', 'p', 'v', 'm')
          AND c.relname NOT IN ('alembic_version', 'spatial_ref_sys')
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
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
