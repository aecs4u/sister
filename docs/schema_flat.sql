-- Prints a Graphviz description of schema `sister` (tables, columns, foreign keys) from the catalog.
-- usage: psql "$DSN" -At -f docs/schema_flat.sql > docs/schema_flat.dot && dot -Tsvg docs/schema_flat.dot -o docs/schema_flat.svg
SELECT 'digraph sister {' || E'\n' || '  rankdir=LR; fontname="Helvetica"; node [shape=plaintext fontname="Helvetica" fontsize=10]; edge [color="#6b7280" arrowsize=0.6];';
SELECT format('  %I [label=<<table border="0" cellborder="1" cellspacing="0" cellpadding="3"><tr><td bgcolor="%s"><b>%s</b></td></tr>%s</table>>];',
               c.relname,
               CASE WHEN c.relname IN ('comune', 'cadastral_unit', 'subject', 'right_kind', 'ownership_right') THEN '#dbeafe'
                    WHEN c.relname IN ('request_type', 'document_type', 'cadastral_category', 'land_quality', 'activity_action',
                                       'land_registry_office') OR c.relname LIKE 'mortgage\_%type' OR c.relname = 'mortgage_party_quality' THEN '#e5e7eb'
                    ELSE '#fef3c7' END,
               c.relname,
               (SELECT string_agg(format('<tr><td align="left">%s%s <font color="#6b7280">%s</font></td></tr>',
                        CASE WHEN EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid = c.oid AND k.contype = 'p' AND a.attnum = ANY (k.conkey)) THEN '<b>' || a.attname || '</b>' ELSE a.attname::text END,
                        '', format_type(a.atttypid, a.atttypmod)), '' ORDER BY a.attnum)
                FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped))
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'sister' AND c.relkind = 'r' AND c.relname NOT IN ('spatial_ref_sys', 'alembic_version')
ORDER BY c.relname;
SELECT format('  %I -> %I;', regexp_replace(k.conrelid::regclass::text, '^sister\.', ''), regexp_replace(k.confrelid::regclass::text, '^sister\.', ''))
FROM pg_constraint k
WHERE k.contype = 'f' AND k.connamespace = 'sister'::regnamespace
ORDER BY 1;
SELECT '}';
