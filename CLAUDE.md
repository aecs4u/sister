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
  - `utils.py` — SISTER portal browser automation (run_visura, run_visura_soggetto, etc.)
  - `client.py` — VisuraClient async HTTP client
  - `cli.py` — Typer CLI with query, db, and top-level commands
- **`tests/`** — pytest test suite
- **`alembic/`** — Database migrations
- **`data/`** — documenti, dossier e output generati
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
