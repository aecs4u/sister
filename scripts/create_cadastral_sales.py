#!/usr/bin/env python3
"""Versioned, additive cross-database cadastral_sales migration and Neon export.

Run with the project's Python. Local connections use libpq/.pgpass; Neon uses
CADASTRAL_SALES_POSTGRES_DSN_NEON from the environment or the project's .env.
No source records are changed. See docs/cadastral_sales.md for the contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = "_cadastral_sales"
MARKER = "cadastral_sales migration v1; managed by scripts/create_cadastral_sales.py"
RELATIONS = ("cadastral_parcel", "cadastral_sheet")


def log(message):
    print(message, flush=True)


def local(database, *, autocommit=False):
    connection = psycopg.connect(
        host="127.0.0.1",
        port=5432,
        user="postgres",
        dbname=database,
        connect_timeout=15,
        application_name="cadastral_sales_migration",
        autocommit=autocommit,
    )
    connection.execute("SET statement_timeout = '30min'")
    connection.execute("SET jit = off")
    connection.execute("SET extra_float_digits = 3")
    if not autocommit:
        connection.commit()
    return connection


def owned_schema(connection, name):
    existing = connection.execute(
        "SELECT obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname=%s", (name,)
    ).fetchone()
    if existing and existing[0] != MARKER:
        raise RuntimeError(f"Refusing to adopt unmanaged schema {name}")
    connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(name)))
    connection.execute(sql.SQL("COMMENT ON SCHEMA {} IS {}").format(sql.Identifier(name), sql.Literal(MARKER)))
    connection.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(name)))


def ensure_database(connection, database):
    if not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        log(f"Created database {database}")


def foreign_source(connection, database, source_schema, names, target_schema):
    """Use a private, fixed TCP loopback mapping; never log credentials."""
    owned_schema(connection, target_schema)
    connection.execute("CREATE EXTENSION IF NOT EXISTS postgres_fdw")
    server = "cadastral_sales_" + database
    existing = connection.execute("SELECT srvoptions FROM pg_foreign_server WHERE srvname=%s", (server,)).fetchone()
    options = {"host=127.0.0.1", "port=5432", f"dbname={database}", "fetch_size=20000"}
    if existing and set(existing[0]) != options:
        raise RuntimeError(f"Foreign server {server} has unexpected options")
    if not existing:
        connection.execute(
            sql.SQL(
                "CREATE SERVER {} FOREIGN DATA WRAPPER postgres_fdw "
                "OPTIONS (host '127.0.0.1', port '5432', dbname {}, fetch_size '20000')"
            ).format(sql.Identifier(server), sql.Literal(database))
        )
    # libpq has already read the matching .pgpass entry for this connection.
    password = connection.info.password
    if not password:
        raise RuntimeError("The local FDW mapping requires the existing libpq password")
    connection.execute(
        sql.SQL(
            "CREATE USER MAPPING IF NOT EXISTS FOR CURRENT_USER SERVER {} OPTIONS (user 'postgres', password {})"
        ).format(sql.Identifier(server), sql.Literal(password))
    )
    for name in names:
        exists = connection.execute("SELECT to_regclass(%s)", (f"{target_schema}.{name}",)).fetchone()[0]
        if not exists:
            connection.execute(
                sql.SQL("IMPORT FOREIGN SCHEMA {} LIMIT TO ({}) FROM SERVER {} INTO {}").format(
                    sql.Identifier(source_schema),
                    sql.Identifier(name),
                    sql.Identifier(server),
                    sql.Identifier(target_schema),
                )
            )


PVP_RAW = """
WITH named_istat AS MATERIALIZED (
    SELECT lower(btrim(name)) AS name, min(cadastral_code) AS cadastral_code
    FROM modelview.istat_municipalities
    GROUP BY 1 HAVING count(DISTINCT cadastral_code)=1
), municipality_codes AS MATERIALIZED (
    SELECT p.code, p.name, p.province_code, coalesce(i.cadastral_code,n.cadastral_code) AS cadastral_code
    FROM modelview.map_pvp_municipalities p
    LEFT JOIN modelview.istat_municipalities i ON i.pro_com::text = ltrim(p.code, '0')
    LEFT JOIN named_istat n ON lower(btrim(p.name))=n.name
), fallback AS MATERIALIZED (
    SELECT lower(btrim(m.name)) AS name, upper(btrim(m.province_code)) AS province,
           lower(btrim(p.name)) AS province_name,
           min(m.cadastral_code) AS cadastral_code
    FROM municipality_codes m
    LEFT JOIN modelview.map_pvp_provinces p ON p.code=m.province_code
    GROUP BY 1,2,3 HAVING count(DISTINCT m.cadastral_code)=1
)
SELECT 'pvp_enriched'::text AS source_database,
       'modelview.modelview_registries'::text AS source_relation,
       r.id::text AS source_id,
       coalesce(i.cadastral_code, address_municipality.cadastral_code, f.cadastral_code)::text AS municipality_code,
       r.section::text AS section, r.sheet::text AS sheet, r.parcel::text AS parcel
FROM modelview.modelview_registries r
JOIN modelview.modelview_assets a ON a.id=r.asset_id
LEFT JOIN modelview.modelview_addresses address ON address.id=a.address_id
LEFT JOIN modelview.istat_municipalities i
  ON i.pro_com::text=ltrim(address.municipality_code,'0')
LEFT JOIN municipality_codes address_municipality ON address_municipality.code=address.municipality_code
LEFT JOIN modelview.modelview_sales sale ON sale.id=a.sale_id
LEFT JOIN fallback f ON i.cadastral_code IS NULL AND address_municipality.cadastral_code IS NULL
 AND lower(btrim(sale.city))=f.name
 AND (upper(btrim(sale.province))=f.province OR lower(btrim(sale.province))=f.province_name)
"""

SISTER_RAW = """
SELECT 'sister'::text AS source_database, 'sister.cadastral_unit'::text AS source_relation,
       cadastral_unit_id::text AS source_id, comune_code::text AS municipality_code,
       section::text, sheet::text, parcel::text
FROM sister.v_cadastral_unit
"""

OPENDATA_RAW = """
WITH documents AS (
    SELECT 'opendata.cadastral_queries'::text AS source_relation, id, result::jsonb AS payload
    FROM opendata.cadastral_queries
    UNION ALL
    SELECT 'opendata.cadastral_prospects', id, result::jsonb
    FROM opendata.cadastral_prospects
), nodes AS (
    SELECT d.*, node.value, node.ordinality
    FROM documents d
    CROSS JOIN LATERAL jsonb_path_query(d.payload,
       'strict $.** ? (exists(@.foglio) || exists(@.sheet))')
       WITH ORDINALITY AS node(value,ordinality)
)
SELECT 'opendata'::text AS source_database, source_relation,
       id || ':' || ordinality::text AS source_id,
       coalesce(nullif(value->>'codice_catastale',''), nullif(value->>'municipality_code',''),
                CASE WHEN split_part(value->>'comune','#',1) ~ '^[A-Z][0-9]{3}$'
                     THEN split_part(value->>'comune','#',1) END,
                CASE WHEN split_part(payload #>> '{parametri,comune}','#',1) ~ '^[A-Z][0-9]{3}$'
                     THEN split_part(payload #>> '{parametri,comune}','#',1) END)::text AS municipality_code,
       coalesce(nullif(value->>'sezione',''),nullif(value->>'sezione_urbana',''),
                value->>'section',payload #>> '{parametri,sezione}')::text AS section,
       coalesce(value->>'foglio',value->>'sheet')::text AS sheet,
       coalesce(value->>'particella',value->>'parcel')::text AS parcel
FROM nodes
"""

NORMALIZE = r"""
CREATE OR REPLACE FUNCTION _cadastral_sales.components(value text)
RETURNS text[] LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
      WHEN btrim(coalesce(value,'')) ~* '^[0-9]+[A-Z]*(_[A-Z])?([[:space:]]*([,;-]|[[:space:]]+E[[:space:]]+)[[:space:]]*[0-9]+[A-Z]*(_[A-Z])?)*$'
      THEN ARRAY(SELECT CASE WHEN token ~ '^[0-9]+$' THEN coalesce(nullif(ltrim(token,'0'),''),'0')
                             ELSE regexp_replace(token,'^0+([0-9])','\1') END
                 FROM regexp_split_to_table(upper(btrim(value)),
                       '[[:space:]]*([,;-]|[[:space:]]+E[[:space:]]+)[[:space:]]*') AS token)
      ELSE ARRAY[]::text[] END
$$;
CREATE OR REPLACE FUNCTION _cadastral_sales.sheet_reference(code text, section text, sheet text)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN upper(btrim(code)) ~ '^[A-Z][0-9]{3}$'
                     AND upper(btrim(coalesce(section,''))) ~ '^([A-Z0-9]|_)?$'
                     AND sheet ~ '^[0-9]{1,4}[A-Z]?(_[A-Z])?$'
       THEN upper(btrim(code)) || coalesce(nullif(upper(btrim(section)),''),'_')
         || lpad(substring(sheet FROM '^[0-9]+'),4,'0')
         || coalesce(nullif(substring(sheet FROM '^[0-9]+([A-Z])'),''),'0')
         || coalesce(nullif(substring(sheet FROM '_([A-Z])$'),''),'0')
       ELSE NULL END
$$;
CREATE OR REPLACE VIEW _cadastral_sales.reference_audit AS
WITH raw AS (
    SELECT * FROM _cadastral_sales_pvp.raw_references
    UNION ALL SELECT * FROM _cadastral_sales_sister.raw_references
    UNION ALL SELECT * FROM _cadastral_sales_opendata.raw_references
), parsed AS MATERIALIZED (
    SELECT *, _cadastral_sales.components(sheet) AS sheets,
              _cadastral_sales.components(parcel) AS parcels FROM raw
), expanded AS MATERIALIZED (
    SELECT p.source_database,p.source_relation,p.source_id,p.municipality_code,p.section,
           p.sheet AS raw_sheet,p.parcel AS raw_parcel,
           s.sheet, v.parcel,
           CASE WHEN cardinality(p.sheets)>1 AND cardinality(p.parcels)>1 THEN NULL
                ELSE _cadastral_sales.sheet_reference(p.municipality_code,p.section,s.sheet) END AS sheet_reference
    FROM parsed p
    LEFT JOIN LATERAL unnest(p.sheets) AS s(sheet) ON true
    LEFT JOIN LATERAL unnest(p.parcels) AS v(parcel) ON true
)
SELECT *, CASE WHEN sheet_reference IS NOT NULL AND parcel IS NOT NULL
              THEN sheet_reference || '.' || parcel END AS national_cadastral_reference,
       CASE WHEN municipality_code IS NULL OR upper(btrim(municipality_code)) !~ '^[A-Z][0-9]{3}$' THEN 'missing_municipality'
            WHEN upper(btrim(coalesce(section,''))) !~ '^([A-Z0-9]|_)?$' THEN 'unresolved_section'
            WHEN sheet_reference IS NULL THEN 'invalid_or_ambiguous_sheet'
            WHEN parcel IS NULL THEN 'sheet_only_or_unparsed_parcel'
            ELSE 'valid_reference' END AS reference_status
FROM expanded;
CREATE OR REPLACE VIEW _cadastral_sales.parcel_keys AS
SELECT DISTINCT national_cadastral_reference AS reference,
       (('x' || substr(md5('cadastral-parcels:' || national_cadastral_reference),1,13))::bit(52)::bigint+1) AS id
FROM _cadastral_sales.reference_audit WHERE national_cadastral_reference IS NOT NULL;
CREATE OR REPLACE VIEW _cadastral_sales.sheet_keys AS
SELECT DISTINCT sheet_reference AS reference,
       (('x' || substr(md5('cadastral-sheets:' || sheet_reference),1,13))::bit(52)::bigint+1) AS id
FROM _cadastral_sales.reference_audit WHERE sheet_reference IS NOT NULL;
"""


def prepare():
    for database, query in (("pvp_enriched", PVP_RAW), ("sister", SISTER_RAW), ("opendata", OPENDATA_RAW)):
        with local(database) as connection:
            owned_schema(connection, SUPPORT)
            connection.execute(
                sql.SQL("CREATE OR REPLACE VIEW {}.raw_references AS ").format(sql.Identifier(SUPPORT)) + sql.SQL(query)
            )
        log(f"Installed {database} live reference view")
    with local("cadastral") as connection:
        owned_schema(connection, SUPPORT)
        for database, schema in (("pvp_enriched", "pvp"), ("sister", "sister"), ("opendata", "opendata")):
            foreign_source(connection, database, SUPPORT, ["raw_references"], f"{SUPPORT}_{schema}")
        connection.execute(NORMALIZE)
        tables = connection.execute("""
            SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind IN ('r','p')
              AND c.relname ~ '^[a-z][a-z_]*__(particelle|fogli)$' ORDER BY 1
        """).fetchall()
        # Reuse the exact public projection of each regional branch from the
        # existing canonical views, including its IDs, typmods and provenance.
        for relation, suffix, kind, ref_column, key_view in (
            ("cadastral_parcel", "__particelle", "parcels", "national_cadastral_reference", "parcel_keys"),
            ("cadastral_sheet", "__fogli", "sheets", "national_zoning_reference", "sheet_keys"),
        ):
            original = connection.execute(
                "SELECT pg_get_viewdef(%s::regclass,true)", (f"spatial.{relation}",)
            ).fetchone()[0]
            # Filtering the existing projection by a source regional CTE keeps
            # its schema identical without copying any geometry rows.
            ctes = [
                sql.SQL("keys AS MATERIALIZED (SELECT * FROM {}.{})").format(
                    sql.Identifier(SUPPORT), sql.Identifier(key_view)
                )
            ]
            for (table,) in tables:
                if table.endswith(suffix):
                    identity = f"(('x' || substr(md5('cadastral-{kind}:' || p.{ref_column}),1,13))::bit(52)::bigint+1)"
                    ctes.append(
                        sql.SQL("""{} AS (
                        SELECT candidate.* FROM keys k JOIN LATERAL (
                            SELECT p.* FROM public.{} p WHERE {}=k.id AND p.{}=k.reference OFFSET 0
                        ) candidate ON true
                    )""").format(
                            sql.Identifier(table), sql.Identifier(table), sql.SQL(identity), sql.Identifier(ref_column)
                        )
                    )
            # pg_get_viewdef qualifies tables only if needed. Redirect either
            # representation to the selected regional CTE, avoiding recursion.
            for (table,) in tables:
                original = original.replace(f"public.{table}", f'"{table}"')
            connection.execute(
                sql.SQL("CREATE OR REPLACE VIEW {}.{} AS WITH ").format(
                    sql.Identifier(SUPPORT), sql.Identifier(relation)
                )
                + sql.SQL(",\n").join(ctes)
                + sql.SQL("\n")
                + sql.SQL(original.rstrip().rstrip(";"))
            )
        log("Installed cadastral source-side filters using existing identity indexes")
    with local("postgres", autocommit=True) as admin:
        ensure_database(admin, "cadastral_sales")
    with local("cadastral_sales") as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        foreign_source(connection, "cadastral", SUPPORT, (*RELATIONS, "reference_audit"), "_cadastral_source")
        connection.execute("CREATE SCHEMA IF NOT EXISTS spatial")
        for relation in RELATIONS:
            existing = connection.execute(
                "SELECT obj_description(to_regclass(%s),'pg_class')", (f"spatial.{relation}",)
            ).fetchone()[0]
            exists = connection.execute("SELECT to_regclass(%s)", (f"spatial.{relation}",)).fetchone()[0]
            if exists and existing != MARKER:
                raise RuntimeError(f"Refusing to replace unmanaged spatial.{relation}")
            connection.execute(
                sql.SQL("CREATE OR REPLACE VIEW spatial.{} AS SELECT * FROM _cadastral_source.{}").format(
                    sql.Identifier(relation), sql.Identifier(relation)
                )
            )
            connection.execute(
                sql.SQL("COMMENT ON VIEW spatial.{} IS {}").format(sql.Identifier(relation), sql.Literal(MARKER))
            )
    log("Local cadastral_sales now exposes two live spatial views")


def columns(connection, relation):
    return connection.execute(
        """SELECT attname,format_type(atttypid,atttypmod)
        FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum
    """,
        (f"spatial.{relation}",),
    ).fetchall()


def copy_query(relation, column_specs):
    ordering = [
        "source_release",
        "canonical_reference" if relation == "cadastral_parcel" else "sheet_reference",
        "inspire_localid",
    ]
    return sql.SQL("COPY (SELECT {} FROM spatial.{} ORDER BY {},geom::text) TO STDOUT").format(
        sql.SQL(",").join(sql.Identifier(c) for c, _ in column_specs),
        sql.Identifier(relation),
        sql.SQL(",").join(sql.SQL('{} COLLATE "C"').format(sql.Identifier(c)) for c in ordering),
    )


def snapshot(directory):
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"migration": MARKER, "snapshot_started_at": datetime.now(timezone.utc).isoformat(), "relations": {}}
    with local("cadastral_sales") as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for relation in RELATIONS:
            specs = columns(connection, relation)
            digest = hashlib.sha256()
            path = directory / f"{relation}.copy"
            count = 0
            log(f"Exporting live spatial.{relation}")
            with path.open("wb") as output, connection.cursor() as cursor:
                with cursor.copy(copy_query(relation, specs)) as copy:
                    for block in copy:
                        output.write(block)
                        digest.update(block)
                        count += bytes(block).count(b"\n")
            manifest["relations"][relation] = {
                "columns": specs,
                "rows": count,
                "bytes": path.stat().st_size,
                "sha256": digest.hexdigest(),
            }
            log(f"Exported {relation}: {count:,} rows, {path.stat().st_size:,} bytes")
        manifest["selection_audit"] = selection_audit(connection, directory, manifest)
    manifest["snapshot_completed_at"] = datetime.now(timezone.utc).isoformat()
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def selection_audit(connection, directory, manifest):
    """Verify membership against the same FDW transaction as both exports."""
    references = {}
    for relation in RELATIONS:
        with (directory / f"{relation}.copy").open("rb") as source:
            # Identity fields contain no COPY escapes; retain all polygon parts
            # in the artifacts while comparing identities as sets here.
            references[relation] = {line.split(b"\t", 2)[1].decode() for line in source}
        manifest["relations"][relation]["distinct_references"] = len(references[relation])
    log("Auditing source membership in the snapshot transaction")
    audit = {}
    known_parcels, known_sheets = set(), set()
    with connection.cursor(name="selection_audit") as cursor:
        cursor.itersize = 20000
        cursor.execute("""SELECT source_database,reference_status,national_cadastral_reference,sheet_reference
            FROM _cadastral_source.reference_audit""")
        for database, status, parcel, sheet in cursor:
            entry = audit.setdefault(
                database, {"reference_status_counts": {}, "parcel_keys": set(), "sheet_keys": set()}
            )
            entry["reference_status_counts"][status] = entry["reference_status_counts"].get(status, 0) + 1
            if parcel:
                entry["parcel_keys"].add(parcel)
                known_parcels.add(parcel)
            if sheet:
                entry["sheet_keys"].add(sheet)
                known_sheets.add(sheet)
    if not references["cadastral_parcel"] <= known_parcels or not references["cadastral_sheet"] <= known_sheets:
        raise RuntimeError("Export contains an identity absent from the live source references")
    for database, entry in audit.items():
        for kind, relation in (("parcel", "cadastral_parcel"), ("sheet", "cadastral_sheet")):
            keys = entry.pop(f"{kind}_keys")
            entry[f"distinct_{kind}_references"] = len(keys)
            entry[f"matched_{kind}_references"] = len(keys & references[relation])
            entry[f"unmatched_{kind}_references"] = len(keys - references[relation])
        log(f"{database}: {entry['matched_parcel_references']:,} distinct parcel references matched")
    audit["parcel_parent_sheets_missing_geometry"] = len(
        {ref.split(".", 1)[0] for ref in references["cadastral_parcel"]} - references["cadastral_sheet"]
    )
    return audit


def neon_config():
    settings = {**dotenv_values(ROOT / ".env"), **os.environ}
    dsn = settings.get("CADASTRAL_SALES_POSTGRES_DSN_NEON")
    if not dsn:
        raise RuntimeError("CADASTRAL_SALES_POSTGRES_DSN_NEON is missing")
    params = conninfo_to_dict(dsn)
    if params.get("dbname") != "cadastral_sales" or not params.get("host", "").endswith(".neon.tech"):
        raise RuntimeError("The dedicated Neon DSN must select cadastral_sales on neon.tech")
    params.update(connect_timeout="20", application_name="cadastral_sales_migration")
    return params


def publish(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["migration"] != MARKER or set(manifest["relations"]) != set(RELATIONS):
        raise RuntimeError("Unexpected snapshot manifest")
    # Validate both artifacts before touching the remote database.
    for relation, spec in manifest["relations"].items():
        with (directory / f"{relation}.copy").open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != spec["sha256"]:
                raise RuntimeError(f"Snapshot checksum failed for {relation}")
    params = neon_config()
    admin_params = {**params, "dbname": "neondb"}
    with psycopg.connect(make_conninfo(**admin_params), autocommit=True) as admin:
        ensure_database(admin, "cadastral_sales")
    with psycopg.connect(make_conninfo(**params)) as connection:
        connection.execute("SET statement_timeout='30min'")
        connection.execute("SET extra_float_digits=3")
        connection.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        connection.execute("CREATE SCHEMA IF NOT EXISTS spatial")
        connection.execute("SELECT pg_advisory_xact_lock(hashtext('cadastral_sales_migration'))")
        for relation, spec in manifest["relations"].items():
            existing = connection.execute("SELECT to_regclass(%s)", (f"spatial.{relation}",)).fetchone()[0]
            if existing:
                comment = connection.execute(
                    "SELECT obj_description(%s::regclass,'pg_class')", (f"spatial.{relation}",)
                ).fetchone()[0]
                if comment != MARKER or [list(c) for c in columns(connection, relation)] != spec["columns"]:
                    raise RuntimeError(f"Refusing to overwrite unmanaged or incompatible spatial.{relation}")
            else:
                connection.execute(
                    sql.SQL("CREATE TABLE spatial.{} ({})").format(
                        sql.Identifier(relation),
                        sql.SQL(",").join(
                            sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(t)) for c, t in spec["columns"]
                        ),
                    )
                )
                connection.execute(
                    sql.SQL("COMMENT ON TABLE spatial.{} IS {}").format(sql.Identifier(relation), sql.Literal(MARKER))
                )
        # Both tables are replaced and verified in the same transaction.
        connection.execute("TRUNCATE spatial.cadastral_parcel,spatial.cadastral_sheet")
        for relation, spec in manifest["relations"].items():
            log(f"Loading Neon spatial.{relation}")
            with (directory / f"{relation}.copy").open("rb") as source, connection.cursor() as cursor:
                with cursor.copy(sql.SQL("COPY spatial.{} FROM STDIN").format(sql.Identifier(relation))) as copy:
                    while block := source.read(1024 * 1024):
                        copy.write(block)
            for field, method in (
                ("geom", "gist"),
                ("id", "btree"),
                ("national_cadastral_reference" if relation == "cadastral_parcel" else "sheet_reference", "btree"),
            ):
                connection.execute(
                    sql.SQL("CREATE INDEX IF NOT EXISTS {} ON spatial.{} USING {} ({})").format(
                        sql.Identifier(f"{relation}_{field}_idx"),
                        sql.Identifier(relation),
                        sql.SQL(method),
                        sql.Identifier(field),
                    )
                )
            if relation == "cadastral_parcel":
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS cadastral_parcel_lookup_idx ON spatial.cadastral_parcel (municipality_code,sheet,parcel)"
                )
            connection.execute(sql.SQL("ANALYZE spatial.{}").format(sql.Identifier(relation)))
            rows = connection.execute(
                sql.SQL("SELECT count(*) FROM spatial.{}").format(sql.Identifier(relation))
            ).fetchone()[0]
            digest = hashlib.sha256()
            with connection.cursor() as cursor, cursor.copy(copy_query(relation, spec["columns"])) as copy:
                for block in copy:
                    digest.update(block)
            if rows != spec["rows"] or digest.hexdigest() != spec["sha256"]:
                raise RuntimeError(f"Neon validation failed for {relation}; transaction will roll back")
            log(f"Verified Neon {relation}: {rows:,} rows and full SHA-256 match")
        manifest["neon_verified_at"] = datetime.now(timezone.utc).isoformat()
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    log("Committed both verified Neon tables")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "snapshot", "publish"))
    parser.add_argument("--snapshot-dir", type=Path, default=Path("/tmp/cadastral_sales_snapshot"))
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
    elif args.action == "snapshot":
        snapshot(args.snapshot_dir)
    else:
        publish(args.snapshot_dir)


if __name__ == "__main__":
    main()
