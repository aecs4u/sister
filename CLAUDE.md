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
  - `form_config.py` — `/web/forms` groups (single-step params are generated from `query_forms.py`)
  - `models.py` — Pydantic input models, dataclasses, exceptions
  - `db_models.py` — SQLModel ORM table classes
  - `database.py` — Async SQLAlchemy engine, SQLModel sessions, cache functions
  - `utils.py` — SISTER portal browser automation (run_visura, run_visura_soggetto, etc.); `run_visura` is two-phase (see "Data extraction")
  - `result_parsers.py` — pure parsers for the composite portal cells (`RA/103`, `Proprieta' per 1/2`, name + birth data, `visImmSel`, owner radios)
  - `xml_ingest.py` — visura XML → typed document tables (`building_*`, `land_*`, `property_groups`, `ownership_mutations`, `property_owners`, …) + backfill
  - `client.py` — VisuraClient async HTTP client
  - `cli.py` — Typer CLI with query, db, and top-level commands
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
uv run sister query soggetto --cf RSSMRI85E28H501E --wait
uv run sister query workflow --preset due-diligence -P Roma -C ROMA -F 100 -p 50
uv run sister db init
# Fill the structured tables from data already stored (responses JSON, XML documents); idempotent, writes to the app schema
uv run sister db backfill            # projections + xml; `xml --force --limit N` to redo some documents
# Owner <-> property graph (resumable; see docs/dossier_graph.md)
../.venv/bin/python scripts/build_dossier_graph.py --seed <CF|PIVA>
```

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
- uvicorn runs with `--reload` over every `*.py` (also `scripts/`, `tests/`): editing Python while a batch/crawler
  runs restarts the service and drops the request in flight. A restart does **not** log out (CDP mode).
- Operational endpoints need `X-API-Key` when `API_KEY` is set (CLI: `VISURA_API_KEY`).
- Web UI (`/web/*`, `/profile`) **fails closed** (`sister/web.py::_require_auth`): browsers are redirected to `/auth/login?next=…`,
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
- Front-end assets are self-hosted in `sister/static/vendor/` (official Bootstrap 5.3.2, Font Awesome 6.5.0, Tabulator 6.3.1,
  Chart.js 4.4.4 — do not use crowdaction's trimmed CSS copies). No third-party requests remain (fonts included).
- `OPENDATA_API_URL` (workflow storage, separate service) is called with a 1 s connect / 4 s total timeout and a 30 s circuit
  breaker (`web._opendata_get`); when it is down `/web/workflows` shows a notice instead of stalling.
- Environment: keep `httpx` below 1.0 (`pyproject.toml` pins `<1`; 1.0 pre-releases have no `AsyncClient` and break `VisuraClient`).
  Test baseline 2026-10-09 (`/opt/venv/.aecs4u_venv/bin/python -m pytest -p no:logfire --continue-on-collection-errors`):
  813 passed, 52 failed, 4 collection errors (2026-10-09, after the two-phase/extraction work). DB tests run on a throwaway local schema (`tests/pg_isolation.py`: `fresh_db` fixture; skipped
  without a local PostgreSQL; never touches the app schema). All remaining failures/errors are roadmap features absent from `sister/`:
  browser dispatch (12), client contract (5), ontology (3), workflows (3), request jobs (9), section-extraction jobs (16),
  `no_match` result status (4), and 4 test files importing missing helpers (`find_best_option_matches`, `_run_with_network_json`, `_request_job_outcome`).
- The interactive API docs (`/docs`, `/redoc`, `/openapi.json`) require sign-in like the web UI (`main._docs_auth`); `/health` stays public.
- Saved results are kept in the database indefinitely. `RESPONSE_TTL_SECONDS` (6 h) only governs the in-memory cache; deleting
  database rows is opt-in via `DB_RETENTION_SECONDS` (default 0 = never). (It used to reuse the cache TTL, which silently purged
  jobs/responses older than 6 h every minute.) After switching `DATABASE_DSN` to an empty database, rebuild jobs/responses from
  `outputs/` with the admin "Importa output" button on `/web/results`; it is idempotent.
- Logins: `LOCAL_USERS_FILE` (in `.env`) points at `local_users.sister.txt` — hashed, mode 600, git-ignored (`local_users*.txt`). Regenerate or add users with `scripts/hash_local_password.py <user>`; plain entries still work but log a warning.
- Static files: SISTER's own `/static` assets revalidate via ETag (no long cache); only versioned `aecs4u-theme` assets are immutable.
  Presentation formatting (dates, money, enum labels) lives in `sister/display.py` as Jinja filters.
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
CLI batch, `/web/forms` and the generic API route are generated from it and submit through
`VisuraClient.submit`. To add a query or an input, edit that table (see `docs/query_forms.md`); do not hand-write
CLI options or web params. `uv run python -m pytest tests/test_query_forms.py` checks the spec against the
saved portal forms in `tests/fixtures/portal_forms/`.

## Data extraction (two phases)

- `run_visura` (`search` / `intestati`) reads **all HTML first** (immobili list, then the Intestati page of every immobile,
  `_read_immobili_intestati`: no CAPTCHA, the list stays valid after *Indietro*) and **requests documents second**
  (`_request_visura_documents`: the *Tipo di visura* form is the only CAPTCHA page, and SISTER forgets the list after each
  *Inoltra*). `richiedi_documenti=false` (CLI `--richiedi-documenti false`) stops after phase 1. One *Visura per Soggetto* per immobile,
  never the same codice fiscale twice per run; a CAPTCHA nobody solves ends as `needs_human` with `documents_pending`.
- `save_response` projects the JSON into the tables via `database._project_response`: owners are tied to *their* property
  through `result_id` (the owner↔property views join on it), composite cells are split with `result_parsers`, terreni use the
  portal's `ha/are/ca` and `Reddito dominicale/agrario` columns, each row keeps its own catasto. Downloaded XML also fills the
  typed tables (`xml_ingest.ingest_visura_xml`, called from `_persist_flattened_xml`). Details: `docs/data_extraction.md`.
- **When a query's flow changes, regenerate its flowchart**: `cd docs && dot -Tsvg <name>.dot -o <name>.svg` (edit the `.dot`).
  `property_visura_workflow` covers `search`/`intestati`, `person_search_workflow` the persona-fisica flow; the per-preset SVGs in
  `sister/static/images/workflows/` follow the `aecs4u_workflow` step lists.
- No schema change was made: the catasto comune code (`visImmSel`, XML `CodiceComune`) has no column in `visura_properties` yet
  (needs an Alembic migration + `DATABASE_REVISION` bump, which breaks `init_db` until `alembic upgrade head` is run).
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
- Python 3.11+
- Async throughout (psycopg, async SQLAlchemy, asyncio)
- CLI commands use `asyncio.run()` to call async client methods
- f-string log calls avoided in favor of `logger.info("msg: %s", var)` style
