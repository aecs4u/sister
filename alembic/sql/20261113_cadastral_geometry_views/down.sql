DROP VIEW IF EXISTS __SISTER_SCHEMA__.v_cadastral_source_sheet_geometry;
DROP VIEW IF EXISTS __SISTER_SCHEMA__.v_cadastral_source_parcel_geometry;
DROP FOREIGN TABLE IF EXISTS cadastral_source.cadastral_sheet;
DROP FOREIGN TABLE IF EXISTS cadastral_source.cadastral_parcel;
DROP SCHEMA IF EXISTS cadastral_source;
DROP USER MAPPING IF EXISTS FOR postgres SERVER cadastral_server;
DROP SERVER IF EXISTS cadastral_server;
