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
3. **`/browser`** shows the state. *Start* attaches to an existing session first and only falls back to a full
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
`VISURA_API_KEY`; the `/forms` proxy uses `API_KEY` itself.


## Lessons from the bulk runs of 2026-10-26 (about 1,300 searches)

- **A session lasts about 30 minutes from the login, whatever the activity** (29.6, 29.8 and 30.4 min measured). The keep-alive does
  not extend it. Run bulk work with `scripts/run_in_session.py` (see the file's docstring): it stops cleanly at `--minutes` after the
  login, so the service never reaches the expiry. After the expiry the portal answers with `Servizi/sessione_scaduta.jsp` or `lock.html`.
- **The service must not log in by itself.** aecs4u-auth's keep-alive tests the province menu on the *current* page, which result pages
  do not have, and marks a healthy session invalid; the package then tried a "recovery" and a full CIE login in the user's tab, which
  waits for a phone approval and blocks the worker. `browser._ensure_authenticated` now only checks and recovers (back to the Visure
  page) and fails with "Sessione SISTER scaduta o non valida" otherwise (`SISTER_AUTO_RELOGIN=true` restores the package behaviour).
  A real search, not the reading of the open page, is the proof that a session is alive (`run_in_session.py` runs one first).
- **After a click, wait for the next page, not for a load state.** `click()` returns when the click is dispatched; `wait_for_load_state`
  then returns at once on the page being left. `utils._click_nav` waits for the navigation itself. The shortcut from the left menu waits
  for the form's own select; before that, one search in six started the full path on top of a navigation still in flight and ended on
  SceltaServizio (the pages of failed rows are kept in `logs/errors/`).
- **"NESSUNA CORRISPONDENZA" is an answer, not an error** (`search_outcome.classify`). `query batch --resume` does not send it again
  (`batch.FINAL_ERRORS`), and `v_subject_search_outcome` lists every code with its outcome (`found`, `no_match`, `invalid_code`, `too_many`, `pending`, `error`).
  A codice fiscale with a wrong check character (`fiscal_code.valid_codice_fiscale`) gets SISTER's "Codice Fiscale non valido!! si vuole
  continuare ugualmente?" page; it is reported as no match and never confirmed.
- **Where the time goes:** the server's answer (~1.2 s per page, 4-6 pages per search, 5-6 searches a minute at best). Blocking or
  caching static files and waiting for `load` instead of `networkidle` gave no measurable gain (the second one raised the error rate).
- **Process hygiene:** `pkill -f` matches the shell that runs it; stop services and batches by pid. A service with a pending login ignores
  SIGTERM and needs SIGKILL. The shared Chrome (`--remote-debugging-port=9222`) must be running before `ade_login.py`.
- **Never open a second tab on a live SISTER session to test it:** the search that followed got `lock.html` (2026-10-27). The proof of a session is the service attach plus one real search (`run_in_session.py`; `--probe` is opt-in).

## Lessons from the bulk runs of 2026-10-27

- **Check `request.cadastre_type` of a bulk run, not only the outcome.** The generic route `/visura/{search_type}` defaulted `tipo_catasto` to
  `T`, so 380 `soggetto-immobili` requests (26-27/10) searched Terreni only; 335 subjects were redone-worthy (107 found without fabbricati,
  228 "nessuna corrispondenza" that was not final). Subject searches default to `E` now (`soggetto-immobili`, `soggetto-documento`), the
  cache key includes the cadastre type, `run_in_session.answered_codes` counts only answers with `E` (or none) and `prune_plan` writes
  `tipo_catasto: E` into the plan rows. A "no match" is final only for the type that was searched.
- **Heavy legal entities** can exceed the action timeout even at 45 s; some searches may leave no request row or saved page.
  because `dump_error_page` covers only `esegui_visura_soggetto`, not the persona giuridica / `soggetto_immobili` flow, and the fast page log
  (`SISTER_PAGE_LOG_FAST`) skips HTML. Options: retry with `SISTER_ACTION_TIMEOUT_SECONDS=120`, or let the user load the Elenco Province /
  A controlled, read-only CDP inspection of the Elenco Province / Immobili page is an alternative when a query cannot finish.
- **Short VAT numbers** (9-10 digits, Genova NPL list) are queried zero-padded to 11 digits and checked with `fiscal_code.valid_partita_iva`;
  compare a list with the database by joining the padded code to `v_subject_search_outcome`. Since revision `20261105_subject_search_outcome_v2` the
  view takes the code from the job payload when the answer carried no subject (SISTER's "Superato il limite massimo di 400 elementi", queued
  requests), so placeholder VAT codes are `too_many`; outcomes are `found`, `no_match`, `invalid_code`, `too_many`,
  `pending`, `error`, and `searched_types` lists the catasto types searched (T, F, E).
- **Progress bar:** `run_in_session.py` shows `batch_progress.Dashboard` while it runs: total over all plans, SISTER session time left (red under
  3 min), rows still possible in this session, then one bar per plan. Without a terminal it prints one line a minute. `batch_progress.py`
  alone watches the journals from another terminal.
- **"Tipo richiesta" (Attualità / Storica) exists on the subject forms** (radio `tipo_richiesta`, `A` / `S`, Attualità by default; seen on the live persona
  fisica form on 2026-10-27, the older saved forms in `logs/pages` did not have it). `tipo_richiesta=storica` (CLI `--tipo-richiesta`, batch param) is set by
  `utils._select_tipo_richiesta` for `soggetto`, `soggetto-immobili` and `soggetto-documento` (persona fisica and giuridica); it raises when the form has no
  such control instead of answering with Attualità. It is part of the cache key. The database does not record it yet, so an Attualità answer and a Storica
  one of the same code look alike to `run_in_session.answered_codes`. Plan with it: `scripts/build_batch_from_list.py <list.md> --output-dir <dir> --tipo-richiesta storica`.
  The layout of a Storica result page is unverified: test one row for real before a full run.
