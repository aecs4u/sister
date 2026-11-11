-- Remove only the abandoned, zero-row import tables in localhost sister.empty.
-- Take a full pg_dump first. Run atomically with:
-- psql -X -w -h 127.0.0.1 -U postgres -d sister -v ON_ERROR_STOP=1 \
--   --single-transaction -f scripts/one_off/drop_unused_local_schema.sql
-- Recovery: restore schema empty from that dump using pg_restore --schema=empty.
-- No CASCADE: unexpected dependencies or remaining objects abort the operation.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $cleanup$
DECLARE
    expected_tables constant text[] := ARRAY[
        'cadastral_inspections', 'cadastral_location_parameters',
        'cadastral_property_properties', 'cadastral_prospect_owners',
        'cadastral_prospect_properties', 'cadastral_queries',
        'feedback_config', 'feedback_config_items', 'feedback_unsubscribes',
        'immobili', 'intestati', 'legal_entity_search_entities',
        'legal_entity_search_geo_summaries', 'legal_entity_search_parameters',
        'legal_entity_search_properties', 'sezioni_extraction_jobs'
    ];
    expected_sequences constant text[] := ARRAY[
        'sister_pkseq_20261006_cadastral_location_parameters',
        'sister_pkseq_20261006_cadastral_prospect_owners',
        'sister_pkseq_20261006_cadastral_prospect_properties',
        'sister_pkseq_20261006_feedback_config',
        'sister_pkseq_20261006_immobili', 'sister_pkseq_20261006_intestati',
        'sister_pkseq_20261006_legal_entity_search_geo_summaries',
        'sister_pkseq_20261006_legal_entity_search_properties'
    ];
    relation_list text;
    relation_name text;
    populated boolean;
BEGIN
    IF current_database() <> 'sister' THEN
        RAISE EXCEPTION 'This cleanup is only for the sister database';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('sister.drop_unused_local_schema'));
    IF to_regnamespace('empty') IS NULL THEN
        RAISE NOTICE 'Schema empty is already absent';
        RETURN;
    END IF;

    IF EXISTS (
        SELECT FROM pg_class c
        WHERE c.relnamespace = 'empty'::regnamespace
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
          AND NOT (
              (c.relkind = 'r' AND c.relname::text = ANY(expected_tables))
              OR (c.relkind = 'S' AND c.relname::text = ANY(expected_sequences))
          )
    ) THEN
        RAISE EXCEPTION 'Unexpected relations in empty; inspect before cleanup';
    END IF;

    SELECT string_agg(format('empty.%I', c.relname), ', ' ORDER BY c.relname)
    INTO relation_list
    FROM pg_class c
    WHERE c.relnamespace = 'empty'::regnamespace AND c.relkind = 'r';

    IF relation_list IS NOT NULL THEN
        -- Hold locks from the emptiness check through the drops, preventing a
        -- concurrent insert from being lost after the check.
        EXECUTE 'LOCK TABLE ' || relation_list || ' IN ACCESS EXCLUSIVE MODE';
        FOR relation_name IN
            SELECT c.relname FROM pg_class c
            WHERE c.relnamespace = 'empty'::regnamespace AND c.relkind = 'r'
        LOOP
            EXECUTE format('SELECT EXISTS (SELECT FROM empty.%I)', relation_name)
                INTO populated;
            IF populated THEN
                RAISE EXCEPTION 'Refusing to remove nonempty table empty.%', relation_name;
            END IF;
        END LOOP;
        -- A single DROP permits foreign keys among these tables, while
        -- RESTRICT protects dependencies belonging to any retained object.
        EXECUTE 'DROP TABLE ' || relation_list || ' RESTRICT';
    END IF;

    FOREACH relation_name IN ARRAY expected_sequences LOOP
        EXECUTE format('DROP SEQUENCE IF EXISTS empty.%I RESTRICT', relation_name);
    END LOOP;
    DROP SCHEMA empty RESTRICT;
    RAISE NOTICE 'Removed the unused empty schema; retained schemas are untouched';
END
$cleanup$;
