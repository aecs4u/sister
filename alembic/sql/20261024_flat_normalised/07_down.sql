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
DECLARE r record;
BEGIN
    FOR r IN
        SELECT c.relname, c.relkind
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'sister' AND c.relkind IN ('r', 'p', 'v', 'm')
          AND c.relname NOT IN ('alembic_version', 'spatial_ref_sys')
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
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
