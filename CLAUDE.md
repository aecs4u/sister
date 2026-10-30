# SISTER — Claude Code Instructions

## Project overview

SISTER is a FastAPI service + Typer CLI for automated cadastral data extraction from the Italian SISTER portal (Agenzia delle Entrate). It uses browser automation via `aecs4u-auth` for SPID/CIE authentication and Playwright for portal navigation.

## Architecture

- **`sister/`** — Python package (FastAPI app + CLI)
  - `main.py` — FastAPI app, lifespan, route registration
  - `routes.py` — Route handler functions
  - `services.py` — VisuraService (queue, worker, cache)
  - `browser.py` — BrowserManager: attaches to the SISTER session, dispatches the portal automations
  - `query_forms.py` — single definition of every single-step query (see "Single-step queries")
  - `form_config.py` — `/forms` groups (single-step params are generated from `query_forms.py`)
  - `models.py` — Pydantic input models, dataclasses, exceptions
  - `db_models.py` — SQLModel ORM table classes
  - `database.py` — Async SQLAlchemy engine, SQLModel sessions, cache functions
  - `utils.py` — SISTER portal browser automation (run_visura, run_visura_soggetto, etc.); `run_visura` is two-phase (see "Data extraction")
  - `result_parsers.py` — pure parsers for the composite portal cells (`RA/103`, `Proprieta' per 1/2`, name + birth data, `visImmSel`, owner radios)
  - `xml_ingest.py` — visura XML → typed document tables (`building_*`, `land_*`, `property_groups`, `ownership_mutations`, `property_owners`, …) + backfill
  - `cadastral_values.py` / `omi_quotes.py` / `land_registry_kpis.py` / `valuation_docs.py` — valore catastale (IMU base) and indicative OMI estimate
    on the subject-visura page, and the data behind `/valuation`. OMI quotes come from the local `omi` database (`public` schema;
    `OMI_POSTGRES_DSN`); OMI has no land quotes, so terreni get the cadastral value only. Details and gotchas: `docs/valuation.md`
  - `omi_zone_overview.py` — `/omi/valuation` zone overview: OMI quote history, variations, yield, successor hint, ISTAT/MEF/Eurostat
    context from the `omi` and `aecs4u-stats` databases (`docs/valuation.md`)
  - `pdf_typst.py` + `typst/visura.typ` + `GET /documents/<id>/pdf` — PDF of a visura (subject or property), typeset with **Typst** (`typst` package, no browser):
    `build_context` turns the page data (xml_parsed, `doc.view`, `subject_land_registry_kpis`, parcel geometries) into plain JSON, the template only reads
    `data.json`, so no visura text is ever Typst source. Browser print uses `static/css/pages/document_visura.css` (A4 landscape); the theme's blocked
    `onclick="window.print()"` is now `data-aecs4u-print` (theme.js) with a fallback in `sister_actions.js`. Page-level polish (labels, headings, dates, aria) is `static/js/document_visura.js`.
  - `client.py` — VisuraClient async HTTP client
  - `batch.py` — `query batch` engine: JSON/CSV submission file → validated plan → runner with retries, journal and `--resume` (format: `docs/batch_format.md`, schema `docs/batch_submission.schema.json`)
  - `mcp_server.py` — optional local MCP server over stdio; wraps `VisuraClient` and `query_forms.py`
  - `cli.py` — Typer CLI with query, db, and top-level commands
  - `building_kmz.py` + `building_overrides.json` — OSM footprints → 3D KMZ (extruded volumes, roads, optional gabled roofs);
    served by `/3d/kmz`, drawn by `/3d` (`templates/building.html`, `static/js/building_kmz_viewer.js` with self-hosted
    three.js; "Vista realistica" uses the procedural textures in `building_textures.js`). Verified facts (floors, roof ridges,
    facade palette) per OSM way go in the overrides file; the Google 3D tiles view needs `GOOGLE_MAPS_API_KEY` (see `.env.example`).
    Google Earth/Street View imagery is only looked at by eye, never traced or copied into the model.
- **`tests/`** — pytest test suite
- **`alembic/`** — Database migrations
- **`data/`** — documenti, dossier e output generati
- **`docs/*.dot` / `*.svg`** — flowcharts of the query flows (`property_visura_workflow`, `person_search_workflow`); `docs/data_extraction.md` lists what each form returns and where it is stored
- **`scripts/`** — Start script + bulk query scripts
  - `query_recrowd_proponents.py` — bulk `/visura/persona-giuridica` for all Recrowd proponents
  - `ade_login.py` — SISTER login in the shared Chrome (`--close` frees a stale session)
  - `build_dossier_graph.py` — owner ↔ property graph crawler (`docs/dossier_graph.md`)
  - `build_pdf_parseable_batch.py` / `run_pdf_parseable_batch.sh` — batch for PDFs without a parseable companion (`docs/pdf_parseable_batch.md`)

## Key commands

```bash
# Log in to SISTER first (CIE approval); the service only attaches to that session
../.venv/bin/python scripts/ade_login.py            # --close frees a stale session
# Start service
./scripts/start.sh

# Run tests
uv run python -m pytest

# CLI
uv run sister health
uv run sister query search -P Roma -C ROMA -F 100 -p 50 --wait
uv run sister query soggetto --cf CF_DI_ESEMPIO --wait
uv run sister query workflow --preset due-diligence -P Roma -C ROMA -F 100 -p 50
uv run sister db init
# Fill the structured tables from data already stored (responses JSON, XML documents); idempotent, writes to the app schema
uv run sister db backfill            # projections + xml; `xml --force --limit N` to redo some documents
# Optional local MCP server for LLM clients
uv sync --extra mcp
uv run sister-mcp
# Owner <-> property graph (resumable; see docs/dossier_graph.md)
../.venv/bin/python scripts/build_dossier_graph.py --seed <CF|PIVA>
```

## MCP server

- Setup and client configuration: `docs/mcp_server.md`. The server is optional and uses local stdio; it does not start
  the browser or log in to SISTER. The API service and an authenticated portal session must already be running.
- Query discovery and validation come from `query_forms.py`; submissions go through `VisuraClient.submit`.
- Keep the `list_queries` → `preview_query` → explicit user confirmation → `submit_query` flow. Previews expire after
  15 minutes and are single-use. The normal submission path reuses an earlier answer when available; `force=true`
  bypasses that behavior and may incur another fee, so use it only when requested.
- `get_request` and `search_history` can return personal and property data. Treat their results as sensitive. CAPTCHA
  solving remains a human action in the active browser session.

## Development conventions

- Package manager: `uv` (not pip directly)
- Run commands with `uv run` prefix
- Tests: `../.venv/bin/python -m pytest -p no:logfire --continue-on-collection-errors` (`uv run` and the logfire plugin fail in this checkout); fixtures in `tests/fixtures/` (`portal_forms/` = saved SISTER forms, `single_step/` = anonymised results)
- Service default port: 8025
- Database: PostgreSQL configured with `DATABASE_DSN`
- All internal imports use relative imports (e.g., `from .database import ...`)
- CLI entry point: `sister` (registered in pyproject.toml as `sister = "sister.cli:run"`)
- Auth config: `.env` file with `ADE_USERNAME`, `ADE_PASSWORD`, `ADE_AUTH_METHOD=cie`

## Portal session and running jobs

- The service **never logs in by itself**: it attaches to the SISTER session left open in the shared Chrome
  (`BROWSER_CDP_ENDPOINT`) by `scripts/ade_login.py`. One session per user ("Utente gia' in sessione" → `ade_login.py --close`);
  it expires after about 30 minutes and is lost when Chrome restarts. Details: `docs/portal_session.md`.
- `scripts/start.sh` runs without `--reload` by default so edits cannot restart a live demo or batch process. Opt in to
  hot reload for local UI work with `scripts/start.sh --reload`. Reload watches every `*.py` (including
  `scripts/` and `tests/`) and can drop an in-flight request; do not use it for batches or crawlers. A restart does
  **not** log out (CDP mode).
- **A SISTER session lasts ~30 minutes from the login, whatever the activity** (measured 2026-10-26: 29.6, 29.8 and 30.4 min;
  the keep-alive does not extend it). Run bulk work with `scripts/run_in_session.py --batch <dir> ...` right after the login:
  it checks the browser, starts the service, drops answered rows and wrong codici fiscali from the plans, proves the session with
  one real search, runs `query batch --resume` until `--minutes` (26) after the login or the first expired-session answer,
  interrupts cleanly and stops the service. `scripts/batch_progress.py <dir>...` shows the progress bar.
- **The service never logs in by itself** (`SISTER_AUTO_RELOGIN=false`, `browser._ensure_authenticated`). aecs4u-auth's keep-alive
  marks a healthy session invalid whenever its province-menu test runs on a result page, and its answer to that was a recovery and then
  a CIE login in the user's tab, waiting for a phone approval nobody expected and blocking the worker. The check now tries the
  recovery navigation (back to the Visure page) and otherwise fails with "Sessione SISTER scaduta o non valida".
- `query batch --resume` does not resend a row whose answer was SISTER's own "NESSUNA CORRISPONDENZA" (`batch.FINAL_ERRORS`);
  a codice fiscale with a wrong check character (`sister/fiscal_code.py`) is answered by SISTER with a confirmation page and
  is reported the same way, never confirmed. A failed search keeps its page in `logs/errors/<request id>.html`.
- **A subject search must cover Fabbricati and Terreni (`tipo_catasto=E`).** The generic route `/visura/{search_type}` used to default to `T`,
  so every `soggetto-immobili` of 2026-10-26/27 (335 subjects) listed only Terreni and a "nessuna corrispondenza" was not final (a subject with
  fabbricati only got it). Now `E` is the default for `soggetto-immobili`/`soggetto-documento`; `run_in_session.answered_codes` ignores answers
  with another `cadastre_type`, and `prune_plan` writes `tipo_catasto: E` into the plan rows. `run_in_session.py` shows a live bar
  (all plans, SISTER session time left, rows still possible; `batch_progress.Dashboard`).
- **"Tipo richiesta" (Attualità / Storica)** of the subject forms is `tipo_richiesta` (`attualita`|`storica`, default the portal's Attualità), set by
  `utils._select_tipo_richiesta`, which raises if the form lacks the control; `scripts/build_batch_from_list.py --tipo-richiesta storica` puts it in a plan.
- Speed: the time of a search is SISTER's own answer (~1.2 s per page, 4-6 pages). Measured 2026-10-26: no gain from blocking/caching
  static files (`SISTER_CACHE_STATIC`, off) nor from waiting for `load` instead of `networkidle` (`SISTER_FAST_NAV`, off: more errors);
  `SISTER_PAGE_LOG_FAST` (on) skips the HTML/screenshot of every step, `SISTER_ACTION_TIMEOUT_SECONDS` (15) shortens a failing row.
- Operational endpoints need `X-API-Key` when `API_KEY` is set (CLI: `VISURA_API_KEY`).
- Web UI routes (including `/`, `/profile`) **fail closed** (`sister/web.py::_require_auth`): browsers are redirected to `/auth/login?next=…`,
  other clients get 401. Only an explicit `REQUIRE_AUTHENTICATION=false` (dev) lets anonymous requests through; `.env` is `true`
  and `tests/conftest.py` sets the switch for the suite. Local logins: `local_users.txt` (outside the repo).
- **Templates must not use inline event handlers** (`onclick="..."`): the CSP blocks them (`script-src` needs the per-request nonce,
  no `'unsafe-inline'`). Use `data-click|change|input|dragover|dragleave|drop="fn.path"` (+ `data-args='[...]'`, see
  `static/js/sister_actions.js`) and put `nonce="{{ csp_nonce }}"` on every inline `<script>` (macros take it as a parameter).
  Fonts and all JS/CSS libraries are self-hosted (`sister/static/vendor/`); do not add CDN URLs (CSP `connect-src`/`script-src` are `'self'`).
- Hardening: CSRF for cookie-authenticated browser requests (`sister/csrf.py`, token derived from the auth cookie; the
  `csrf_token`/`csrf_input()` template helpers and `static/js/sister_csrf.js` add it), per-path rate limits (`/auth/*` 20/min,
  rest 600/min, per socket peer), nonce-based CSP outside `/docs` (`sister/security.py`), opt-in admin gating via
  `SISTER_ADMIN_USERS` (Browser Control, imports, rescans), hashed passwords in `local_users.txt` (`scripts/hash_local_password.py`).
  Sign-in/out/password-help pages are SISTER's own (`sister/auth_pages.py`), mounted before the package's.
- Core front-end assets are self-hosted in `sister/static/vendor/` (official Bootstrap 5.3.2, Font Awesome 6.5.0,
  Tabulator 6.3.1, Chart.js 4.4.4 — do not use crowdaction's trimmed CSS copies; fonts are local too). Map views request
  tiles from OpenStreetMap, Esri and OpenTopoMap; the Google 3D map is loaded only after the visitor chooses it. Subject
  map fallbacks may geocode unmapped parcel addresses in the background; `/3d` address searches run on form submission
  or when opening an address-specific link. Results are cached server-side; geocoding can be disabled with
  `SISTER_GEOCODING=off`.
- `OPENDATA_API_URL` (workflow storage, separate service) is called with a 1 s connect / 4 s total timeout and a 30 s circuit
  breaker (`web._opendata_get`); when it is down `/workflows` shows a notice instead of stalling.
- Environment: keep `httpx` below 1.0 (`pyproject.toml` pins `<1`; 1.0 pre-releases have no `AsyncClient` and break `VisuraClient`).
  Test baseline 2026-10-11 (`/opt/venv/.aecs4u_venv/bin/python -m pytest -p no:logfire --continue-on-collection-errors`):
  863 passed, 52 failed, 4 collection errors (825 passed with the same 52/4 on 2026-10-10). DB tests run on a throwaway local schema (`tests/pg_isolation.py`: `fresh_db` fixture; skipped
  without a local PostgreSQL; never touches the app schema). All remaining failures/errors are roadmap features absent from `sister/`:
  browser dispatch (12), client contract (5), ontology (3), workflows (3), request jobs (9), section-extraction jobs (16),
  `no_match` result status (4), and 4 test files importing missing helpers (`find_best_option_matches`, `_run_with_network_json`, `_request_job_outcome`).
- The interactive API docs (`/docs`, `/redoc`, `/openapi.json`) require sign-in like the web UI (`main._docs_auth`); `/health` stays public.
- Saved results are kept in the database indefinitely. `RESPONSE_TTL_SECONDS` (6 h) only governs the in-memory cache; deleting
  database rows is opt-in via `DB_RETENTION_SECONDS` (default 0 = never). (It used to reuse the cache TTL, which silently purged
  jobs/responses older than 6 h every minute.) After switching `DATABASE_DSN` to an empty database, rebuild jobs/responses from
  `outputs/` with the admin "Importa output" button on `/results`; it is idempotent.
- Logins: `LOCAL_USERS_FILE` (in `.env`) points at `local_users.sister.txt` — hashed, mode 600, git-ignored (`local_users*.txt`). Regenerate or add users with `scripts/hash_local_password.py <user>`; plain entries still work but log a warning.
- Static files: SISTER's own `/static` assets revalidate via ETag (no long cache); only versioned `aecs4u-theme` assets are immutable.
  Presentation formatting (dates, money, enum labels) lives in `sister/display.py` as Jinja filters.
- **`/persons/<cf>` and `/companies/<piva>` are one "Visura Soggetto" page** (`catasto/entity.html` → local `catasto/visura_soggetto_archive.html`,
  groups per comune with the Fabbricati/Terreni tables). The saved soggetto query (*elenco immobili*, `web._subject_query_profile`) is the
  top-level source: `web._subject_tree_with_elenco` adds its units as groups ("da elenco", one per comune and F/T) to whatever a stored
  visura lists, and the visura wins for the units it has (classamento, owners, ownership timelines, KPIs/OMI are computed on the
  visura's own tree). Mappe: the map shows every parcel of the elenco; visure, planimetrie, ispezioni and owners per parcel are the
  "Documenti per particella" section (`subject_hub.documented_parcels`, only parcels with something archived). Without a visura the
  page is the elenco alone (`_render_query_only_subject`). Elenco groups are split like a visura's, by comune, F/T and the subject's
  right and share, and carry a synthetic `IntestazioneGruppo`, so the valuation (valore catastale, quota/diritto) runs on them
  (OMI estimates need parcel positions: only for the visura's own buildings). The groups are `<details>` accordions
  (`visura_global_filter.js` opens/hides them with the filter). The archive summary (`subject_hub.summary`: coverage cards, provinces,
  what is still to request, other searches) replaces the theme's stat cards. `/persons|/companies/<code>/pdf[?document=]` is the PDF of
  that very page (`as_pdf` in `_web_subject_profile`), the PDF button is always injected by `catasto/entity.html`.
- **Parcel map** (`land_registry` `document_parcel_map.js`): the installed package (0.1.4, root-owned) lacks the no-outline fix and
  marker clustering, so `main.py` serves SISTER's copy from `sister/static/land_registry/` at the package URLs; the source is
  `../land-registry` (copy it over after editing; drop the override when a release has both). The hub map looks every parcel up in
  chunks in a background task (`web._hub_parcel_map`, first chunk 40 parcels, each finished chunk is published at once and served as increments
  `?after=N` by long poll; the script draws them as they arrive, `applyData`), data at `/<persons|companies>/<code>/parcel-map.json?k=<key>` (rebuilt from the saved search when the key is gone, e.g. after a restart). View and layers sit in the URL
  (`?map=lat,lng,zoom&layers=l-sheets,…`).
- **Shared templates live in `aecs4u-theme`** (`catasto/forms|results|ispezione_ipotecaria|omi_valuation|visura_*.html` (`omi_valuation` = `/omi/valuation`, built from `components/omi_zone_overview.html`), `components/{hero,nested,timeline,
  table_filter,kpi_card,related_links}.html`); they extend `base_template`, which `main.py` sets to SISTER's `_base.html` (sidebar). Sister
  keeps only SISTER-specific pages. Edit those in `../aecs4u-theme`, then bump the theme in `uv.lock`; SISTER's `display.py` filters are
  required by `catasto/ispezione_ipotecaria.html`.
- UI audit and its fixes: `docs/ui_audit_2026-10-09.md` (§1b = what changed / what is left, mostly in the aecs4u-auth/theme packages).
- Each query sets its SISTER office through `_set_office` (NAZIONALE for persons/companies, the province for
  property queries) and verifies it; a multi-step flow calls it again to change level.

## Known issues

- `query mappa` (EM): Different form layout, submit button selector doesn't match
- `query ispezioni` / `ispezioni-cartacee`: navigation works, result extraction is unverified (ISP returns 0 rows)
- Mandatory form inputs the portal enforces: `sezione` (comuni with sezioni, e.g. Ravenna) for export-mappa/originali,
  `particella` for elaborato, `tipo-nota` for nota; `elenco` needs a second limitation (handled by category groups)
- PostgreSQL migration in progress: SQLModel stores datetimes as timezone-aware UTC (naive values are rejected);
  `visura_properties` / `cadastral_subjects` inserts currently fail with null `id` (does not block results)
- Tests: `tests/test_client_contract.py` (workflow/`reuse_cached`/`request_documents`), `test_browser_dispatch.py`,
  `test_ontology.py` and the DB-backed suites fail or error for features/fixtures not present yet (baseline 2026-10-06: 595 passed, 21 failed, 83 errors)
- Browser automation tests are deprioritized — do not suggest adding them

## CAPTCHA

The SISTER CAPTCHA (`input[name='inCaptchaChars']`) appears only on the document-request form (Tipo di visura →
Inoltra): `/Visure/vimm/InoltraRichiestaVis.do`, `/Visure/vimm/TipoVisura.do`, `/Visure/vpf/TipoVisura.do`. Not seen on
login, navigation, search or result pages; no saved evidence for ispezioni, mappa, originali, fiduciali, nota, partita,
indirizzo or ipotecaria. It is never automated: `_wait_for_captcha` raises `CaptchaRequired`, polling reports
`needs_human`. Details and limits: `docs/captcha_pages.md`.

## Single-step queries

Defined once in `sister/query_forms.py` (portal input → parameter, submit metadata); the CLI commands,
CLI batch, `/forms` and the generic API route are generated from it and submit through
`VisuraClient.submit`. To add a query or an input, edit that table (see `docs/query_forms.md`); do not hand-write
CLI options or web params. `uv run python -m pytest tests/test_query_forms.py` checks the spec against the
saved portal forms in `tests/fixtures/portal_forms/`.

## Data extraction (two phases)

- `run_visura` (`search` / `intestati`) reads **all HTML first** (immobili list, then the Intestati page of every immobile,
  `_read_immobili_intestati`: no CAPTCHA, the list stays valid after *Indietro*) and **requests documents second**
  (`_request_visura_documents`: the *Tipo di visura* form is the only CAPTCHA page, and SISTER forgets the list after each
  *Inoltra*). `richiedi_documenti=false` (CLI `--richiedi-documenti false`) stops after phase 1. One *Visura per Soggetto* per immobile,
  never the same codice fiscale twice per run; a CAPTCHA nobody solves ends as `needs_human` with `documents_pending`.
  A request is `requested` only when the portal says so (`_confirm_request_submitted`: page "Attesa" + "Richiesta inoltrata"; the
  CAPTCHA field disappearing is not proof): otherwise it is `unconfirmed` with a reason, listed in `documents_unconfirmed`.
- **Few page visits per query** (`utils._open_search_form`, `_OFFICE_APPLIED`): the applied office is remembered per tab and the
  left menu (`Persona fisica` / `Persona giuridica`) is on every page of a flow, so a query starts from the page the previous one
  ended on (menu link → form → *Ricerca* → [omonimo] → *Immobili*), not from SceltaServizio + Cambia Ufficio. A different office,
  a dead page (`login.jsp`, `sessione_scaduta`) or a form that does not show falls back to the full path. A subject with several
  provinces goes *Indietro* to the province table for the next one (`run_soggetto_immobili`, `open_list(from_list=True)`) instead of
  repeating the whole search. A `no match` answer costs two loads. The per-request session check of `aecs4u-auth` opens SceltaServizio,
  so `browser._ensure_authenticated` trusts a good check for `SISTER_SESSION_RECHECK_SECONDS` (120, 0 = every request) while the tab is on a
  live SISTER page. Measured 2026-10-26: 4-7 s for a no-match, ~7 s with properties (was 11 s / 13-22 s). Verify changes with a real run.
- `save_response` projects the JSON into the tables via `database._project_response`: owners are tied to *their* property
  through `result_id` (the owner↔property views join on it), composite cells are split with `result_parsers`, terreni use the
  portal's `ha/are/ca` and `Reddito dominicale/agrario` columns, each row keeps its own catasto. Downloaded XML also fills the
  typed tables (`xml_ingest.ingest_visura_xml`, called from `_persist_flattened_xml`). Details: `docs/data_extraction.md`.
- **Only the `.p7m` is stored; its XML is never extracted next to it.** The signed content is read in memory
  (`utils._p7m_payload`, used by `_parse_visura_xml`, the document page, `missing_pairs`, `xml_ingest._complete_content`).
  Older runs left `<stem>.xml` and a bare `<stem>` per `.p7m`, and the folder imports indexed them as extra documents;
  `scripts/cleanup_extracted_xml.py` removes them (dry run by default, verifies each file against the payload, backs up first).
- **Every stored document includes its ISO request date** (`vs_att_<CF>_CE_2026-10-06.p7m`: the date, never the time;
  `utils._unique_document_stem`). A same-day repeat adds the practice id after it (`_2026-10-06_T265733_2026`). A PDF and a
  P7M are separate requests and can carry different dates, so pair them with `utils._document_key(stem)` (the stem without
  the date), never the exact stem. Existing files were renamed with `scripts/rename_documents_with_date.py` (plan + DB
  backup in `<data>/backups/`, `--revert <plan.json>` undoes it). `.meta` files are never renamed.
  `scripts/normalize_documents.py` re-derives names from content (office/comune/`al` date of a subject visura, `sto`/`ter` of
  a property visura), then collapses same-day repeats with the same content (quarantine folder + DB rows, dry run by default)
  and lists the names that still cover different contents. After removing files by hand run
  `scripts/reconcile_missing_documents.py` (dry run by default): it drops the rows/aliases of a deleted `.p7m` only when a
  surviving file has the same XML as the row, never a row that has extraction results its equivalent lacks.
- **`query batch` never re-sends a submitted row on `--resume`** (it re-polls the journaled request ids; a repeat can be paid twice) and
  validates the whole file before sending. After changing `query_forms.py` or `batch.py` models regenerate the schema:
  `sister query batch-schema -o docs/batch_submission.schema.json` (`tests/test_batch.py` fails on drift).
- **`sister query ...` / `query batch` do not silently re-run an answered query** (the service reuses an identical request only
  for `RESPONSE_TTL_SECONDS`, 6 h): with `?reuse_previous=true` it returns the previous answer marked `stale`, and the CLI asks
  "Download it again?" (default no; `--yes` downloads, `--no` reuses, without a terminal the answer is no; `--force` = `--yes`).
- **When a query's flow changes, regenerate its flowchart**: `cd docs && dot -Tsvg <name>.dot -o <name>.svg` (edit the `.dot`).
  `property_visura_workflow` covers `search`/`intestati`, `person_search_workflow` the persona-fisica flow; the per-preset SVGs in
  `sister/static/images/workflows/` follow the `aecs4u_workflow` step lists.
- Schema: `20261010_xml_typed_columns` adds the 7 link columns that `visura_xml_models.py` defined but no migration created
  (`land_parcels.location_id`, `ownership_mutations.reference_location_id`, `property_owners.subject_id/right_id`, …); before it every
  typed XML insert failed on a migrated database and only `document_xml_nodes` was kept. A model column added without a migration
  is invisible to the throwaway test schema (built from the models), so **add the Alembic revision together with the model change**
  and bump `DATABASE_REVISION` only after `alembic upgrade head` has run. Still open: the catasto comune code (`visImmSel`, XML
  `CodiceComune`) has no column in `visura_properties`; `feedback_*` drift comes from `aecs4u-domain` 0.17.0.
- The document page (`/documents/<id>`) renders `doc.view` from `sister/visura_view.py` (current state, history by period,
  ownership acts newest first) through `catasto/visura_story.html` (aecs4u-theme); the generic `xml_parsed` dict is only used for the
  "Dati completi" dump and as fallback. `IndiceMutazione` runs oldest→newest in Terreni and newest→oldest in Fabbricati.
- Browser flow changes in `utils.py` cannot be exercised without a live SISTER session; verify them with a real run
  (`search ... --richiedi-documenti false` first: no CAPTCHA).

## Multi-step due-diligence workflows

See `docs/document_due_diligence_batch.md` for document-derived property jobs and natural-person portfolio jobs, launch order, and paid-step gates. The `portfolio` workflow performs subject lookup, property expansion, ownership-history expansion, timeline, and risk steps; `portfolio_ipotecaria` is a separately gated paid step. A workflow run reuses identical free query submissions and reports cache hits; paid inspections and document downloads are excluded, and `force=true` bypasses reuse. Owner expansion is capped by `min(max_fanout, max_owners)`. Query steps return captured `page_visits` with structured results for normal response persistence. The Richieste step exposes metadata and IDs for rows with a `Salva` link, but omits session-bound download URLs; document downloading remains a separate action.

## Persona giuridica queries

Companies must use `POST /visura/persona-giuridica` with `{"identificativo": "<vat_number>", "tipo_catasto": "E"}` — **not** `/visura/soggetto` (persona fisica). The SISTER portal has separate forms for natural persons vs legal entities.

## Recrowd integration

- DB: configure `RECROWD_DATABASE_DSN` with the Recrowd PostgreSQL connection for this script.
- 97 proponents with VAT numbers; VAT comes from `COALESCE(recrowd_proponent_company_details.vat_code, recrowd_proponents.vat_number)`
- Bulk query: `uv run python scripts/query_recrowd_proponents.py` — queries all 97 sequentially, saves progress to `outputs/recrowd_soggetto_<timestamp>.json`

## Code style

- Line length: 120 (configured in ruff/black)
- Python 3.12+ (`pyproject.toml`)
- Async throughout (psycopg, async SQLAlchemy, asyncio)
- CLI commands use `asyncio.run()` to call async client methods
- f-string log calls avoided in favor of `logger.info("msg: %s", var)` style
