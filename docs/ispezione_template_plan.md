# Ispezione ipotecaria page: show every extracted field — development plan

Status: Stage 1 implemented in `aecs4u-theme` (commit `9ff1a9e`, pushed to
`origin/main`); SISTER's `uv.lock` locally pins that revision. Stage 2 is not
started. No tests have been run for this change.

## Goal

`/documents/<id>` for an ispezione ipotecaria (note, formalities list, title) shows every field that an Ocular
structured extraction produced and SISTER stored, with a truthful label of where the data came from. Nothing an
extraction contains should be reachable only through the raw JSON dump.

## What was surveyed

Source: `/data/aecs4u.it/ocular/outputs/*/final/structured.json` (765 files) and the SISTER database, 2026-11-12.

### Ocular outputs by schema

| Schema (from `provenance.json`) | Files | Relevant here |
|---|---|---|
| `perizia_appraisal_schema` | 592 | no |
| `visura_catastale_schema` 1.0.0 | 131 | no |
| `ispezione_ipotecaria_schema` (all versions) | 28 | **yes** |
| other (elenco immobili, subalterni, marketing, photo, other) | 14 | no |

### The 28 ispezione outputs

| Schema version | Files | Top-level branches with data | Loaded into typed tables |
|---|---|---|---|
| 1.0.0 | 11 | `ispezione`, `nota` | yes (11 extractions) |
| 1.2 (hand-made) | 1 | `ispezione`, `nota` | yes (1 extraction) |
| 1.2.0 | 1 | `ispezione`, `nota` header, `titolo`, `allegati`, `pagine_documento` | **not imported at all** |
| 1.3.0 | 15 (3 documents, several runs/models each) | `elenco_sintetico` | stored as JSON only (6 extractions, 0 typed rows) |

Details that matter for the work:

- The 1.2.0 output (`a2a0e1ca`) is the 24-page title of inspection T 91777 (RG 13722 / RP 10468), i.e. SISTER
  document 2295. It has no row in `document_extraction`.
- The database rows for 1.3.0 point at `…/model_outputs/<model>.json`, not at `final/structured.json`. Each of the
  three documents (2072, 2075, 2083) has two extractions, one per model.
- Nine 1.0.0 outputs name their source without the request date (`isp_…_RP1325.pdf`); match them to documents with
  `utils._document_key`, never the exact file name.
- Document 2291 and the other six notes imported from Richieste on 2026-11-11 have **no Ocular output**. Their only
  extraction is SISTER's own `pdftotext` parse (`sister.ispezione_ipotecaria_pdf`), which fills no typed table.

### Where fields are lost today

Full field inventory (220 leaf paths, with per-version counts) is reproducible with the scan described under
"Verification".

| Group | Fields | Stored | Shown on the page |
|---|---|---|---|
| Inspection | fee paid, page count, note timestamp, number date | typed columns | no |
| Lien | derivation code, annual rate %, semi-annual rate, foreign currency, variable amounts, automatic increase, resolutive condition | typed columns | no |
| Parties | registered office, mortgage domicile, sex | typed columns | no |
| `nota.sezione_a.convenzione` | specie, descrizione, voltura catastale automatica | JSON only | no |
| `nota.sezione_a.dati_riepilogativi` | unit and party counts as printed | JSON only (counts are recomputed) | recomputed value only |
| `nota.sezione_c_raw` | original texts of quality, share, right; `regime` | JSON only | partly (rebuilt from typed columns) |
| `titolo` (1.2.0) | act type and date, repertorio/raccolta, notary, consideration, parties with residence, properties, evidence rows (`dati_rilevati`: field, page, source text) | not imported | no |
| `allegati`, `pagine_documento` (1.2.0) | attachments with page ranges and evidence, full text per page | not imported | no |
| `elenco_sintetico` (1.3.0) | office, request data and restrictions, selected subjects, update periods, formalities with successive formalities, cancellation and related-document flags | JSON only | no |

Titles versus notes: a title (the notarial act, e.g. document 2295) arrives under a cover page reading "Ispezione
… per titolo telematico" and "Nota di Trascrizione - Titolo telematico". SISTER's parser read that as a note; since
2026-11-12 `_parse_ispezione_ipotecaria_pdf` returns `inspection_document_type = "titolo_telematico"` for it (4 of
the 23 stored inspection PDFs). Still open: Ocular's 1.2.0 output for the same title says
`document_kind: nota_dettaglio`. Since 2026-11-13 titles are stored with `document_type = titolo_telematico`
(documents 2073, 2074, 2084, 2295) and fill the "Titolo Telematico (Atto Notarile)" catalogue category;
`web._MORTGAGE_DOCUMENT_TYPES` keeps them on the inspection page and in the subject and parcel inspection lists.

Two further defects on the same page:

- The card is titled "Dati strutturati Ocular · Estrazione automatica" whatever the provider, so SISTER's native
  parse and the hand-made extraction are presented as Ocular output.
- `get_document_structured_extraction` returns only the latest extraction; a document with two (one per model)
  hides the other.

## Work items

Template work is in `../aecs4u-theme` (`catasto/ispezione_ipotecaria.html`); the theme is then bumped in `uv.lock`.

| # | Item | Where | Size |
|---|---|---|---|
| 1 | Label the card from `extraction.provider` / `model` / `schema_name`; native parse gets its own "Estrazione SISTER" heading | theme template | S |
| 2 | Add the typed columns that are stored but not printed (inspection, lien, parties) | theme template | S |
| 3 | Render the `nota` fields that exist only in JSON: `convenzione`, printed `dati_riepilogativi`, `sezione_c_raw` texts and `regime` | theme template | S |
| 4 | Render `elenco_sintetico`: request and restrictions, selected subjects, update periods, formalities table with nested successive formalities and flags | theme template (new macro in `components/`) | M |
| 5 | Render `titolo` and `allegati`: act summary, parties, properties, evidence table with page references; `pagine_documento` as a collapsed per-page text | theme template (new macro) | M |
| 6 | Generic recursive label/value view of the full payload, so a new schema version never hides a field; it can repeat values summarized above, and raw JSON stays as the technical last resort | theme `components/` | S |
| 7 | Extraction picker when a document has more than one: `database.list_document_extractions`, `?extraction=<id>` on the document route, default latest | `sister/database.py`, `sister/web.py`, theme template | M |
| 7b | Treat a title as a title everywhere: heading and badges from `titolo_telematico`, the `titolo` section first, the note header only as "nota di riferimento" (the document type itself is done) | theme template | S |
| 8 | Import the 1.2.0 title output for document 2295 (`sister import-ocular 2295 …/a2a0e1ca/final/structured.json`) after confirming the importer accepts a payload with an empty `nota` | data, `command_tools/import_ocular_extraction.py` if needed | S |
| 9 | Tests: template rendering against anonymised fixtures for 1.0.0, 1.2.0 and 1.3.0 in `tests/fixtures/`; one test that fails when a fixture leaf path is not present in the rendered HTML | `tests/` | M |
| 10 | Docs: `docs/data_extraction.md` section on ispezione extractions; CLAUDE.md line | docs | S |

Order: 1–3 (no data changes), then 6, then 4–5, then 7–8, with 9 growing alongside.

## Out of scope

- Loading `elenco_sintetico` and `titolo` into new typed tables. It needs an Alembic revision and a model design;
  it matters for search and subject links, not for showing the fields.
- Running Ocular on the seven Richieste notes (documents 2291–2294, 2296–2298). Until it runs, those pages show
  only the native parse, correctly labelled after item 1.
- The PDF (`pdf_typst.py`) of the same documents.

## Verification

- Field coverage: for each fixture, every non-empty leaf path of `structured_data` appears in the rendered page
  (item 9). The inventory script walks `…/final/structured.json`, groups by schema version and lists leaf paths;
  it reads only, and is kept with the tests.
- Manual: documents 2071 (note 1.2), 2075 (formalities list 1.3.0, two extractions), 2295 (title 1.2.0 after item
  8), 2291 (native parse only).
- `../.venv/bin/python -m pytest -p no:logfire --continue-on-collection-errors` stays at the current baseline.

## Risks

| Risk | Mitigation |
|---|---|
| Theme change affects other apps using `catasto/ispezione_ipotecaria.html` | New blocks render only when their keys exist; no context variable is renamed |
| Long evidence and page-text blocks make the page heavy; the full field/value view repeats some summarized values | Collapsed `<details>`; page text rendered only on demand in the dedicated Stage 2 section |
| Two extractions exist for each of three documents and may differ | Picker shows one at a time; no merging |
| Personal data in fixtures | Fixtures anonymised like `tests/fixtures/single_step/` |
