# Recrowd analytical historical subject visure

`scripts/batch_recrowd_storica_analitica.py` checks Recrowd proponents against the local Sister PostgreSQL database and submits a national **Storica Analitica per Soggetto** request only when a matching document is not already stored.

The lookup uses each proponent's `tax_code`, then `vat_code`, then Recrowd's `vat_number`. Duplicate proponents with the same identifier are grouped into one request. Proponents without a usable identifier are reported and skipped. In execute mode, a successful recent analytical request that has not yet been downloaded is marked `awaiting_download` to avoid submitting a duplicate.

## Preview

Run from the repository root. The default mode is read-only:

```bash
uv run python scripts/batch_recrowd_storica_analitica.py
```

The job saves a timestamped JSON report under `outputs/`. It identifies an existing document by the queried fiscal code and the analytical historical subtype, using the typed subject, document XML, metadata, and filename where available. A successful recent request that has not yet been downloaded is marked `awaiting_download` to avoid submitting a duplicate.

## Submit requests

First ensure Sister is running with an authenticated SISTER browser session. Then submit a small batch:

```bash
uv run python scripts/batch_recrowd_storica_analitica.py --execute --limit 2
```

Run the full batch with:

```bash
uv run python scripts/batch_recrowd_storica_analitica.py --execute
```

The request uses `tipo_catasto=E`, `vista=analitica`, and searches the identifier as a legal entity. SISTER may ask a human to solve a CAPTCHA. The script stops on `needs_human` and saves its progress report; it does not solve CAPTCHAs.

Documents requested as differita appear under SISTER **Richieste**. To download ready documents into the Sister database, run:

```bash
uv run python scripts/batch_recrowd_storica_analitica.py --execute --download-ready
```

Sister's download endpoint downloads **all** ready items in the account's Richieste page, not just Recrowd requests. The job then rechecks the database and marks matching documents as downloaded.

## Database and API settings

- Sister reads `DATABASE_DSN` from `.env`; configure it for the local Sister database and its schema search path.
- Recrowd reads `RECROWD_DATABASE_DSN` when set. Otherwise the script connects to `postgres` on `localhost`, database `recrowd`, and uses libpq's normal password-file lookup (for example `.pgpass`).
- The API defaults to `http://localhost:8025`; override it with `VISURA_API_URL` or `--sister-url`.
- When the API is protected, set `VISURA_API_KEY` or `API_KEY` in the environment.

The batch is sequential. Its JSON report is written after each processed identifier so an interruption does not discard completed work.

## Upload-ready JSON for the web query submitter

Upload [`inputs/recrowd_soggetto_documento_storica_analitica.json`](../inputs/recrowd_soggetto_documento_storica_analitica.json) in the batch uploader and choose **Ricerca società → Visura storica analitica per soggetto**. It contains 47 unique valid fiscal codes from Recrowd plus the additional code you supplied, `10329400013`, which is not currently linked to a proponent row. None had a matching document in Sister when the file was prepared. The Recrowd database has 109 proponents total; 61 currently lack a fiscal code and are omitted.
