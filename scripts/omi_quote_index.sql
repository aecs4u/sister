-- Run against the `omi` database, outside a transaction (CONCURRENTLY is not allowed in a transaction block).
-- Supports filtering the multi-semester zone table by each OMI comune key.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_omi_ref_zone_comune_period_zone
    ON public.omi_ref_zone (cod_comune_amm, anno DESC, semestre DESC, upper(zona))
    INCLUDE (zona_id, zona);

-- The overview accepts the alternate OMI keys too. Keep each side of its OR predicate indexable.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_omi_ref_zone_comune_catastale
    ON public.omi_ref_zone (cod_comune_catastale);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_omi_ref_zone_comune_istat
    ON public.omi_ref_zone (cod_comune_istat);
