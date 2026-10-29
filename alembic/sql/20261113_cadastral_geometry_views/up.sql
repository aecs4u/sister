CREATE EXTENSION IF NOT EXISTS postgres_fdw WITH SCHEMA __SISTER_SCHEMA__;

CREATE SERVER cadastral_server
    FOREIGN DATA WRAPPER postgres_fdw
    OPTIONS (host '/var/run/postgresql', port '5432', dbname 'cadastral');

CREATE USER MAPPING FOR postgres
    SERVER cadastral_server
    OPTIONS (user 'postgres');

CREATE SCHEMA cadastral_source;

CREATE FOREIGN TABLE cadastral_source.cadastral_parcel (
    id bigint,
    canonical_reference text,
    national_cadastral_reference text,
    parcel text,
    sheet text,
    municipality_code text,
    municipality_name text,
    province text,
    geom __SISTER_SCHEMA__.geometry
)
SERVER cadastral_server
OPTIONS (schema_name 'spatial', table_name 'cadastral_parcel');

CREATE FOREIGN TABLE cadastral_source.cadastral_sheet (
    id bigint,
    sheet_reference text,
    municipality_code text,
    municipality_name text,
    province text,
    geom __SISTER_SCHEMA__.geometry
)
SERVER cadastral_server
OPTIONS (schema_name 'spatial', table_name 'cadastral_sheet');
