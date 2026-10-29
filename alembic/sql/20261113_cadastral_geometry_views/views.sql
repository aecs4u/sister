-- The map requests only cadastral references stored in SISTER. These live views
-- expose the equivalent source rows and leave key filtering to each map query,
-- so postgres_fdw can push municipality/sheet/parcel predicates to cadastral.
CREATE VIEW __SISTER_SCHEMA__.v_cadastral_source_parcel_geometry AS
SELECT id,
       canonical_reference,
       national_cadastral_reference,
       parcel,
       sheet,
       municipality_code,
       municipality_name,
       province,
       geom
FROM cadastral_source.cadastral_parcel;

CREATE VIEW __SISTER_SCHEMA__.v_cadastral_source_sheet_geometry AS
SELECT id,
       sheet_reference,
       municipality_code,
       municipality_name,
       province,
       geom
FROM cadastral_source.cadastral_sheet;
