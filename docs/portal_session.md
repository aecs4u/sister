# SISTER portal session: login, attach, office

## Model

The service does **not** log in to AdE/SISTER on its own. The login lives in a shared Chrome (started with
`--remote-debugging-port`, address in `BROWSER_CDP_ENDPOINT`, e.g. `http://localhost:9222`) and the service
**attaches** to it:

1. **Log in** (CIE approval on your phone): `../.venv/bin/python scripts/ade_login.py` — leaves the SISTER tab
   open on the Visure page and disconnects. If SISTER answers *"Utente gia' in sessione"* the script closes the
   stale session (`CloseSessionsSis` / `CloseSessions`) and retries once. `scripts/ade_login.py --close` only
   closes the session held on the server.
2. **Start the service** (`./scripts/start.sh`). At startup it attaches to a valid SISTER tab
   (`BrowserManager.attach_existing_session`); a tab left on a result page is sent back to Visure without a
   login. With no session the service stays `idle` and queued requests wait.
3. **`/web/browser`** shows the state. *Start* attaches to an existing session first and only falls back to a full
   login when there is none; *Stop* closes the SISTER session on the server (`close_sister_session`) and
   disconnects.

`SISTER_BROWSER_AUTOSTART` no longer exists. `/health` reports `auth_ready` / `state` (`ready`, `idle`,
`connecting`, `unavailable`).

## Things that end the session

| Cause | Effect | Fix |
|---|---|---|
| SISTER session timeout (observed ≈30 min) | pages redirect to `sessione_scaduta.jsp` / `lock.html`; requests fail with *Sessione scaduta* | log in again |
| Chrome restarted | AdE login is a session cookie: it is gone | log in again |
| A second login while one is open | AdE error *"Si prega effettuare la logout…"* / *Utente gia' in sessione* | `scripts/ade_login.py --close` (or "Chiudi" on the portal page), then log in |
| Service **shutdown/reload** | **does not** log out: in CDP mode `graceful_shutdown` only disconnects, so the session survives | none |
| Panel *Stop* | closes the server-side session on purpose | log in again |

One session per user: never open a second login while the first is alive.

## Reloads drop work

`./scripts/start.sh` runs uvicorn with `--reload` over every `*.py` in the project (including `scripts/` and
`tests/`). Saving a `.py` file restarts the service: the request in flight is lost (the client sees
*request_id non trovato* for it) and the service re-attaches. Do not edit Python files while a batch or the dossier
crawler runs; if you must, do it right after a row finishes and re-run the lost row.

## Office level

Every query selects the SISTER *ufficio* through **Cambia Ufficio** (`SceltaServizio.do`, the `listacom`
dropdown), via `_set_office` in `sister/utils.py`:

| Level | Option | Used by |
|---|---|---|
| Nazionale | `NAZIONALE` | person/company search, `soggetto-*` flows, the dossier crawler |
| Provincia | `<PROVINCIA> Territorio` | every property based query (search, intestati, elenco, indirizzo, partita, nota, mappa, export-mappa, originali, fiduciali, elaborato, ispezioni) |

A multi-step automation switches level by calling `_set_office` again; the helper re-navigates to *Cambia
Ufficio*, applies the option and **verifies** the "Ufficio provinciale di:" header, raising when the portal shows a
different office. Each flow starts by setting its office, so no flow inherits a stale one.

## CAPTCHA

Only the document request forms ask for it, and **not every time** (SISTER sometimes serves the form without it
and accepts the submit). While waiting, the tab is brought to the front and the code field is focused. A wrong
code makes SISTER show a new CAPTCHA: the wait repeats (up to 5 attempts, 120 s each) and then raises
`CaptchaRequired` (polling status `needs_human`). See `docs/captcha_pages.md`.

## API key

When `API_KEY` is set the operational endpoints require `X-API-Key`. The CLI/`VisuraClient` read
`VISURA_API_KEY`; the `/web/forms` proxy uses `API_KEY` itself.
