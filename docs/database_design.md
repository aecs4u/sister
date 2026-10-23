# Flat, normalised database design

Status: migration `20261024_flat_normalised` is written and verified on a scratch copy of the database. It has **not**
been applied to the live `sister` database, and the application code (`db_models.py`, `visura_xml_models.py`,
`database.py`, `xml_ingest.py`, `web.py`, the CLI) still targets the old layout. See "What the application must change".

Files: `alembic/versions/20261024_flat_normalised.py` runs the SQL in `alembic/sql/20261024_flat_normalised/`
(`01_dedup` → `02_schema` → `03_load` → `04_validate` → `05_views` → `06_swap`; `07_down` is the downgrade).
ER diagram: `docs/schema_flat.svg` (regenerate with the command at the end).

## Rules

- **Flat**: one typed column per fact. No JSON or text blobs in the working schema, no EAV tree, no `x_json` copy of
  child rows, no 1:1 side tables, no polymorphic "one of these three parents" columns.
- **Normalised**: every real-world thing exists once and is referenced by a `bigint` key (comune, cadastral unit,
  subject, right kind, registry office). Composite source text is split into typed columns: a share `1/2` is two
  integers, `R.Euro:1704,31` is a `numeric`, `dd/mm/yyyy` is a `date`, `RAVENNA (RA)` is a comune.
- **Integrity in the database**: every relation has a foreign key and an index; natural keys are unique
  (`NULLS NOT DISTINCT` where a part may be missing); closed vocabularies are `CHECK`s, open ones are lookup tables.
- **Raw payloads live elsewhere**: response JSON, the XML node tree, extraction JSON and document text moved to
  `sister_archive`. All of it can be rebuilt from the stored `.p7m` files and `outputs/`.

## What was wrong

| Problem | Where | Size |
|---|---|---|
| Same place stored as several rows (`"PALERMO "`, `Palermo`, province `PA` vs `PALERMO`) | `cadastral_locations` | 4290 rows → 3266 units |
| Same real place spelled out again in 8 tables | `building_identifiers`, `land_parcels`, `ownership_mutations`, `related_parcels`, `mortgage_inspection_properties`, … | 8 tables |
| Ownership data in two shapes | `visura_owners` vs `property_owners`; the latter's inline text columns were 100 % blank | 6690 rows |
| Person data in three places | `cadastral_subjects`, `document_subjects` (all columns blank but `subject_id`), mortgage parties | |
| 1:1 tables with a three-way parent column | `building_classifications` / `_identifiers` / `_surfaces` | 1369 parents, each with exactly one of each |
| JSON/EAV copies of data that also exist as columns | `visura_responses.data`, `page_visits.*_json`, `document_xml_nodes`/`_attributes` (270k rows, 58 MB) | |
| Text where a type belongs | shares, dates (`dd/mm/yyyy`), incomes, consistency, planimetria, birth dates | most `text` amounts and dates |
| Dead columns | 50+ always-blank columns | |
| Mixed key types | `text` request ids next to `bigint`/`integer` | |
| 28 `enum_*` tables fed by triggers | accept any value, so they enforce nothing | |

## Deduplication (step 1)

Runs first, is idempotent, and logs every removed row in `dedup_audit.removed` (table, removed id, kept id, reason).
Foreign keys are repointed to the surviving row before anything is deleted.

| Table | Removed | Why |
|---|---|---|
| `cadastral_locations` | 604 | same place after trimming whitespace / folding case |
| `cadastral_subjects` | 10 | no fiscal code, same identity (or same name + birth date as a subject that has one) |
| `property_owners` | 5 | same subject and right twice in one mutation |
| `visura_documents` | 3 (+ their row trees) | same file under `/data/…` and `/mnt/mobile/data/…` (symlink) |

Left alone on purpose: repeated requests (history), the same owner in different responses, 79 groups of documents
with identical content (that is `scripts/normalize_documents.py`, it deletes files), 4 mutation pairs that differ in
`reference_location_id`.

The load then merges more, by normalised key: 420 more units (province spelling), 9 rights, 2 owner rows, 13 geometry
cache rows. These are counted in `04_validate.sql`.

## The new model

```
comune ◄── cadastral_unit ◄── request ──► request_result ──► result_owner ──► subject, ownership_right ──► right_kind
   ▲            ▲    ▲           ▲  ▲                           │
   │            │    └── property ┘  └── page_visit ──► form_element, error
   │            │
   │            ├── document ──► document_subject, document_address, document_extraction ──► mortgage_inspection ──► party ──► subject
   │            │       │                                                                                          └─► property ──► cadastral_unit
   │            │       ├── property_group ──► building_unit ──► building_unit_related_parcel
   │            │       ├── land_parcel ──► land_parcel_classification
   │            │       └── ownership_mutation ──► mutation_owner ──► subject, ownership_right
   └── parcel_geometry_cache
```

| Legacy | New |
|---|---|
| `cadastral_locations`, `geographic_places` | `cadastral_unit` + `comune` (province code, Belfiore code) |
| `cadastral_subjects`, `document_subjects`, mortgage party identity | `subject` (+ `document_subject`) |
| `ownership_right_catalog`, `ownership_rights` | `right_kind`, `ownership_right` (share as numerator/denominator, real dates) |
| `visura_requests` + `visura_responses` | `request` (response columns inline; `data` → `sister_archive.request_payload`) |
| `visura_results`, `visura_properties`, `visura_owners` | `request_result`, `property` (typed amounts), `result_owner` |
| `visura_documents` + `document_metadata` | `document` (relative `file_name`, `land_registry_office`), content → archive |
| `building_addresses`, `property_groups` | `document_address`, `property_group` |
| `building_units` + `building_current_states` + `building_classifications` + `building_identifiers` + `building_surfaces` | `building_unit` (`is_current` distinguishes the current state) |
| `related_parcels` | `building_unit_related_parcel` |
| `land_parcels`, `land_classifications` | `land_parcel`, `land_parcel_classification` |
| `ownership_mutations`, `property_owners` | `ownership_mutation` (always has a `document_id`), `mutation_owner` |
| `page_visits`, `_form_elements`, `_errors` | `page_visit` (no `*_json`), `page_visit_form_element`, `page_visit_error` |
| `activity_log` (jsonb) | `activity_log` + `activity_log_attribute` (one row per leaf), jsonb → archive |
| `document_structured_extractions` | `document_extraction` (jsonb → archive) |
| `mortgage_inspections` + `_liens` + `_titles` | `mortgage_inspection` (one row; `lien_*`, `title_*` columns) |
| `mortgage_inspection_units` + `_properties` | `mortgage_inspection_property` |
| `cadastral_map_geometry_cache` | `parcel_geometry_cache` keyed by `(feature_type, comune_id, section, sheet, parcel)`; `lookup_key` was the concatenation of those columns |
| `document_xml_nodes`, `_attributes` | `sister_archive.document_xml_node`, `_attribute` |
| 28 `enum_*` tables + trigger | `CHECK`s for closed sets; lookup tables for `request_type`, `document_type`, `cadastral_category`, `land_quality`, `activity_action`, mortgage vocabularies |
| `sister_owner_index`, `sister_property_index` (matviews), 13 views | not rebuilt; 7 flat views (below) |

Read views: `v_cadastral_unit`, `v_property`, `v_result_owner`, `v_property_owner` (one row per pair and link scope),
`v_document_cadastral_unit`, `v_cadastral_unit_index`, `v_visura_cadastral_polygon`. The jsonb context builders
(`v_sister_request_context`, `v_sister_document_context`, `v_sister_owner_property_links`) are gone: join the child tables.

## Judgement calls to review

1. **Comuni.** Provinces appear as codes and names (`PA`/`PALERMO`); names map to codes, `NAZIONALE` (an office level) is
   not a province. A name seen with exactly one province gets it where the source left it blank. Belfiore codes come
   from the data; where several names claim a code, and for codes the data never names, `istat.municipalities` decides
   (used only if that schema exists; ISTAT spellings are converted to the portal's: `FORLÌ` → `FORLI'`). A comune seen
   under an old and a new province (Monza MI/MB) is one row under the province that holds its code. Result: 295 comuni,
   294 with a code (`OLBA (SV)` is the exception). Provinces that ISTAT now lists differently (MI→MB, SS→OT) keep the
   province the portal reported.
2. **Shares are reduced**: `1000/1000` → `1/1`, `500/1000` → `1/2`; this merged 9 rights.
3. **Categories** `A02` and `A/2: ABITAZIONI…` are one code `A/2` (with description in `cadastral_category`); classes
   lose leading zeros (`01` → `1`) and have no lookup (open vocabulary).
4. **Dropped as redundant or never populated**: `property_owners` inline text, `land_parcels`/`building_identifiers`/
   `related_parcels` inline location text, `ownership_mutations.ref_*` text (the code lives on `comune`),
   `visura_requests.cost_*`, `visura_properties.province`/`municipality`, mortgage `*_original`, `raw_*`, text interest rate,
   and the derived counts `unit_count`/`party_*_count`. Header fields that the XML defines are kept even where every
   current row is empty or constant (`requester`, `registry_view_type`, `service_type`, `liquidation_units`, historical
   areas, `mutation_date`), so a document that fills them is not silently truncated. Originals remain in `sister_legacy`
   and `sister_archive`.
   `ownership_mutation.is_current` replaces the `mutation_index = 'current'` marker (602 rows) now that the index is an integer.
5. **Mortgage parties become `subject` rows** (3 new subjects; matched by fiscal code first).
6. **Timestamps**: `requested_at` is read as Europe/Rome local time; 21 date-only values become midnight.
7. **Document paths** are stored as `file_name` only; the documents directory is configuration, not data.
8. **`subject.name`** keeps the portal's display name; `last_name`/`first_name` stay optional because the source lacks
   them for 182 persons.
9. **Search matviews** were not rebuilt (`sister_property_index` took over 10 minutes to refresh); rebuild over the new views if wanted.

## What the application must change (not done)

- ORM models: `sister/db_models.py`, `sister/visura_xml_models.py` follow the new tables; the old `OwnershipRightCatalog` becomes `right_kind`.
- Writers: `database._project_response` / `save_response`, `xml_ingest.ingest_visura_xml` and `_persist_flattened_xml` must
  upsert `comune`, `cadastral_unit`, `subject`, `right_kind`, `ownership_right` and the lookup rows (the trigger-fed
  `enum_*` tables no longer exist), parse shares/dates/amounts with `result_parsers`, and write raw payloads to `sister_archive`.
- Readers: `web.py`, `visura_view.py` and the document page read `document`/`building_unit`/`ownership_mutation`; the path
  of a document is `<documents dir>/<file_name>`; map code looks geometry up by comune/section/sheet/parcel.
- Tests: `tests/pg_isolation.py` builds its throwaway schema from the models, so it follows the model change.

## Databases that ran the first variant (including live)

The first variant of `20261024_flat_normalised` was applied to the live database on 2026-10-23. Auditing every lossy
conversion afterwards found two defects: the 602 `mutation_index = 'current'` markers became NULL, and XML header fields that
the visura schema defines (`requester`, `registry_view_type`, `service_type`, `liquidation_units`, historical areas,
`mutation_date`) were dropped as "dead" because every current row was empty or constant. The corrected SQL fixes both for
fresh runs, and `04_validate.sql` now fails if a Belfiore code, an index cast or a marker is lost. (A third suspected defect,
units losing their comune, was checked and does not exist.) Databases that already ran the first variant take
`20261026_flat_normalised_fixes`, which restores both from `sister_legacy` (ids were preserved), ensures the durable request
job table exists on databases already stamped at the earlier revision, and is idempotent. It was checked by reproducing the
first variant on a copy, applying the fix, and comparing it with a database built from the corrected
migration: same columns, mutations, document headers, 3266 units with the same comune codes, same row counts. It was applied to
the live database on 2026-10-23 after a `pg_dump` and `alembic stamp --purge 20261024_flat_normalised` (the live stamp named a
revision that is not in the tree).

The swap and the downgrade now move only the relations named in the migration, so tables added by other migrations
(`sister_request_jobs`, …) stay in `sister`.

## Applying it

1. Back up: `pg_dump -Fc sister > sister_pre_flat.dump` (step 1 deletes rows; downgrade does not restore them).
2. Stop writers (service, batches), then `alembic upgrade head`. It is one transaction: any failed check in
   `04_validate.sql` rolls everything back.
3. The previous layout is in `sister_legacy`. `alembic downgrade -1` swaps it back (rows written after the upgrade are lost).
4. When nothing reads `sister_legacy` any more: `DROP SCHEMA sister_legacy CASCADE`.

Verified on a `pg_dump` copy (about 12 s): 73 reconciliation checks (row counts, NULL-parity of every link, totals of
income/area/file size), upgrade → downgrade → upgrade, and an audit of the result (no JSON columns, a primary key
and an indexed foreign key everywhere, 59 foreign keys, no isolated tables).

## Regenerate the diagram

```bash
psql "$DSN" -At -f docs/schema_flat.sql > docs/schema_flat.dot && dot -Tsvg docs/schema_flat.dot -o docs/schema_flat.svg
```
