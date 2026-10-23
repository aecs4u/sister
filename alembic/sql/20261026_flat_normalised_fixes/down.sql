-- The added columns carry data that also lives in sister_legacy; dropping them restores the first variant of the layout.
ALTER TABLE sister.ownership_mutation DROP COLUMN IF EXISTS is_current;
ALTER TABLE sister.ownership_mutation DROP COLUMN IF EXISTS mutation_date;
ALTER TABLE sister.document DROP COLUMN IF EXISTS registry_view_type;
ALTER TABLE sister.document DROP COLUMN IF EXISTS service_type;
ALTER TABLE sister.document DROP COLUMN IF EXISTS requester;
ALTER TABLE sister.document DROP COLUMN IF EXISTS liquidation_units;
ALTER TABLE sister.document DROP COLUMN IF EXISTS historical_total_area_sqm;
ALTER TABLE sister.document DROP COLUMN IF EXISTS historical_excluded_area_sqm;
