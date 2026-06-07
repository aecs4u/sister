# SISTER — Claude Code Instructions

## Project overview

SISTER is a FastAPI service + Typer CLI for automated cadastral data extraction from the Italian SISTER portal (Agenzia delle Entrate). It uses browser automation via `aecs4u-auth` for SPID/CIE authentication and Playwright for portal navigation.

## Architecture

- **`sister/`** — Python package (FastAPI app + CLI)
  - `main.py` — FastAPI app, lifespan, route registration
  - `routes.py` — Route handler functions
  - `services.py` — BrowserManager, VisuraService (queue, worker, cache)
  - `models.py` — Pydantic input models, dataclasses, exceptions
  - `db_models.py` — SQLModel ORM table classes
  - `database.py` — Async SQLAlchemy engine, SQLModel sessions, cache functions
  - `utils.py` — SISTER portal browser automation (run_visura, run_visura_soggetto, etc.)
  - `client.py` — VisuraClient async HTTP client
  - `cli.py` — Typer CLI with query, db, and top-level commands
- **`tests/`** — pytest test suite
- **`alembic/`** — Database migrations
- **`data/`** — SQLite database (sister.sqlite)
- **`scripts/`** — Start script + bulk query scripts
  - `query_recrowd_proponents.py` — bulk `/visura/persona-giuridica` for all Recrowd proponents

## Key commands

```bash
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
```

## Development conventions

- Package manager: `uv` (not pip directly)
- Run commands with `uv run` prefix
- Tests: `uv run python -m pytest` (158+ tests)
- Service default port: 8025
- Database: SQLite at `data/sister.sqlite` (configurable via `SISTER_DB_PATH`)
- All internal imports use relative imports (e.g., `from .database import ...`)
- CLI entry point: `sister` (registered in pyproject.toml as `sister = "sister.cli:run"`)
- Auth config: `.env` file with `ADE_USERNAME`, `ADE_PASSWORD`, `ADE_AUTH_METHOD=cie`

## Known issues

- `query mappa` (EM): Different form layout, submit button selector doesn't match
- `query ispezioni` / `query ispezioni-cartacee` (ISP/ISPCART): "Passa a Ispezioni" opens a different SISTER module requiring dedicated navigation
- Browser automation tests are deprioritized — do not suggest adding them

## Persona giuridica queries

Companies must use `POST /visura/persona-giuridica` with `{"identificativo": "<vat_number>", "tipo_catasto": "E"}` — **not** `/visura/soggetto` (persona fisica). The SISTER portal has separate forms for natural persons vs legal entities.

## Recrowd integration

- DB: `/mnt/mobile/data/aecs4u.it/classaction/recrowd.sqlite`
- 97 proponents with VAT numbers; VAT comes from `COALESCE(recrowd_proponent_company_details.vat_code, recrowd_proponents.vat_number)`
- Bulk query: `uv run python scripts/query_recrowd_proponents.py` — queries all 97 sequentially, saves progress to `outputs/recrowd_soggetto_<timestamp>.json`

## Code style

- Line length: 120 (configured in ruff/black)
- Python 3.11+
- Async throughout (aiosqlite, async SQLAlchemy, asyncio)
- CLI commands use `asyncio.run()` to call async client methods
- f-string log calls avoided in favor of `logger.info("msg: %s", var)` style
