# Recrowd historical property visure

`scripts/batch_recrowd_immobile_storica_analitica.py` finds properties owned by Recrowd proponents, checks the local Sister PostgreSQL document index, and can submit a **Visura Catastale Storica Analitica per Immobile** for each property without a matching document.

The script groups proponents by tax code and uses `soggetto-immobili` to list their national holdings. Sister's property normalizer supplies the province, municipality, cadastre type, sheet, parcel, section, and subunit used to match the local document metadata. Duplicate property rows are submitted once, with all matching proponents retained in the report.

## Preview

Run from the repository root. This queries property holdings through Sister and checks the local database, but does not request historical visure:

```bash
uv run python scripts/batch_recrowd_immobile_storica_analitica.py
```

Use small limits to inspect a subset:

```bash
uv run python scripts/batch_recrowd_immobile_storica_analitica.py --limit-proponents 2 --limit-properties 5
```

The timestamped JSON report is saved under `outputs/`. It includes proponents, unique property keys and locations, current status, API request IDs, and any rows that could not be parsed. Statuses include `already_present`, `missing_dry_run`, `awaiting_document`, and `request_in_progress`.

## Submit missing requests

Ensure Sister is running with an authenticated SISTER browser session, then submit a small batch first:

```bash
uv run python scripts/batch_recrowd_immobile_storica_analitica.py --execute --limit-proponents 2 --limit-properties 5
```

Run the full batch with:

```bash
uv run python scripts/batch_recrowd_immobile_storica_analitica.py --execute
```

The job submits requests sequentially and saves its JSON report as it proceeds. SISTER may require a human to solve a CAPTCHA; the job stops at that property. Solve it in Sister's authenticated browser, then rerun the job. Stored documents and recent/in-progress matching requests are detected to avoid repeat submissions.

## Upload-ready JSON for the web query submitter

Upload [`inputs/recrowd_visura_storica_analitica_immobili.json`](../inputs/recrowd_visura_storica_analitica_immobili.json) and choose **Cerca immobili → Storica analitica per immobile**. The file has 60 unique locations without a matching Sister report or recent request, sourced from owner-linked Recrowd visure for **ALTER HAUS S.R.L.** and **CHLOE S.R.L.** This is a partial inventory: local Recrowd data has no trustworthy per-property listing for the other 45 of the 47 identifiable proponents. Their subject-property listings need to be discovered before their property-level requests can be assembled.

## Database and API settings

- Sister reads `DATABASE_DSN` from `.env`; it must point to the Sister PostgreSQL database and its schema search path.
- Recrowd reads `RECROWD_DATABASE_DSN` when set. Otherwise the script connects as `postgres` to `localhost/recrowd` and uses libpq's configured password file.
- The API defaults to `http://localhost:8025`; override it with `VISURA_API_URL`, `SISTER_URL`, or `--sister-url`.
- If the API requires a key, set `VISURA_API_KEY` or `API_KEY` in the environment.
