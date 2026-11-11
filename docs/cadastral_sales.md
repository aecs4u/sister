# cadastral_sales

`cadastral_sales` exposes two combined relations with the same columns and
PostGIS geometry types as `cadastral.spatial`:

| Relation | Localhost | Neon |
| --- | --- | --- |
| `spatial.cadastral_parcel` | Live view | Snapshot table |
| `spatial.cadastral_sheet` | Live view | Snapshot table |

The selection is the union of cadastral references in:

- `pvp_enriched.modelview.modelview_registries`, resolved through its asset
  address and municipality, with the sale municipality as a fallback;
- `sister.sister.v_cadastral_unit`, including the normalized units used by
  building-related parcels;
- `opendata.opendata.cadastral_queries` and `cadastral_prospects`, reading
  cadastral objects and query parameters from their JSON results.

All referenced sheets with available geometry are included, including those
whose parcel number is absent or cannot be parsed. A referenced parcel is
included only when its exact national reference exists in the loaded regional
cadastral data. No date/status restriction is applied to sales.

## Identity and missing data

Matching uses municipality cadastral code, cadastral section, sheet, sheet
development/attachment, and parcel. The source national reference is retained.
The existing 52-bit feature ID index supplies candidates, followed by an exact
reference comparison to reject hash collisions. Canonical columns, original
geometry, flags, regional `source_release`, and all polygon parts/duplicates
are preserved. IDs are indexed rather than declared unique, because multiple
polygon parts can share an entity ID.

Explicit numeric parcel lists are expanded and leading numeric zeroes are
normalized. When both sheet and parcel contain multiple identifiers, no
sheet/parcel pairing is inferred. Free text, missing municipality codes and
section names that cannot be translated to the national reference are
reported in the audit. Urban section abbreviations are not assumed to equal
the cadastral section character. A blank section selects the blank-section
reference; it does not select all sections in the municipality.

For historical PVP ISTAT codes absent from the current municipality lookup,
an exact municipality-name match is accepted only when the canonical lookup
maps that name to one cadastral code. Address municipality takes precedence
over sale municipality.

No geometry is invented for unmatched references. Coverage also depends on
what is already loaded in `cadastral`; some regions have sheets but no parcels.
The snapshot manifest reports distinct matched/unmatched references separately
for each database, parsing failures, and missing parent-sheet geometry.

## Installation and refresh

This is an operational, versioned migration across five local databases and a
separate Neon database. It is deliberately independent of the SISTER app's
Alembic revision chain. No application startup runs this migration.

```bash
.venv/bin/python scripts/create_cadastral_sales.py prepare
.venv/bin/python scripts/create_cadastral_sales.py snapshot --snapshot-dir /tmp/cadastral_sales_snapshot
.venv/bin/python scripts/create_cadastral_sales.py publish --snapshot-dir /tmp/cadastral_sales_snapshot
```

Local connections use `127.0.0.1:5432`, `postgres`, and libpq's existing
`.pgpass`. Neon uses `CADASTRAL_SALES_POSTGRES_DSN_NEON` from the environment
or the root `.env`; the configured database must be `cadastral_sales` on a
`neon.tech` host. No credentials are written to the snapshot or report.

`prepare` adds private `_cadastral_sales*` schemas with reference views and
foreign tables to the source databases, plus `_cadastral_source` in the new
database. The public data interface consists of the two `spatial` views;
regional cadastral source tables and source records remain unchanged. Filtering
runs inside `cadastral` using its existing regional identity indexes, before
geometry crosses the foreign-data connection. Private-schema access is revoked
from `PUBLIC`; foreign mappings belong to the migration's database owner.

The migration can be rerun. Managed schemas/relations have ownership comments;
the script refuses to adopt unrelated objects with the same names. Removing
the local target database and the private helper schemas removes this migration;
those schemas do not own application records. Review dependencies before any
manual removal; the script never drops databases or schemas.

`snapshot` reads both local views and the reference audit in one repeatable-read
transaction. Its COPY artifacts preserve the complete rows, and its manifest
records columns/types, counts, SHA-256 checksums, and source coverage. Ordering
uses the `C` collation for portable checksums across local and Neon databases.
Exports can take several minutes because the views resolve the live reference
sets and sheets can contain large geometries.

`publish` creates the Neon database when missing, enables PostGIS, and creates
the two tables with geometry and lookup indexes. A refresh replaces only the
two script-owned tables. Both loads, indexes and full count/checksum verification
run in a single transaction; any failure rolls back both tables. A successful
publish records its verification time in the manifest.

Local views remain live. Neon changes only when the snapshot and publish
commands run; no scheduled refresh is installed.
