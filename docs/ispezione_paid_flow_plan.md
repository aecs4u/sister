# Ispezione ipotecaria: guarded paid flow, structured storage, euro ledger — development plan

Status: **implementation in progress, updated with executor and verification findings (2026-11-16).** Branch
`feature/ispezione-paid-flow` (from `main` at `97e69c0`). The formalità-table pagination (commit `80a3e48`) is complete.

- **Phase 1:** typed tables, payload projection/backfill, typed-first subject reader, pending-request storage, and a
  parser regression case are implemented. A text-only anonymized fixture derived from saved capture 8043 covers its 12
  formalità and checkbox identities; names, register numbers, dates, locations, act descriptions, and selection IDs were
  replaced. `scripts/capture_page.py --parse` parses a read-only capture and records matching checkbox values. Existing
  payloads still use the JSON fallback for formalità when typed rows are absent; their cost fields are suppressed because
  they lack charge provenance. No anonymized raw HTML fixture is checked in.
- **Phase 2:** measured tariff quotes, ownership checks, admin quote controls, and stale-rate blocking are implemented.
  After the local typed-table backfill, the T 79390 quote was checked against stored data: 12 selected, one already owned
  (document 2085), and 11 new notes estimated at 44,00 €; all 11 payable formalities have stored portal selection values.
  This verifies projection and quote logic, not a current portal page. The before/after panel render on a running service
  remains unchecked.
- **Phase 3:** the durable flow/action state, API-key start/status/quote operations, and admin-session-only confirmation
  and reconciliation routes now cover *Accetta*, note/title requests, and one paid Richieste *Salva* by exact
  `idRichiesta`. Note/title requests persist intent before the click and are confirmed only when each quoted formalità
  maps uniquely to a Richieste row by RP/RG. *Salva* reads the exact row, quotes its positive listed price (or a dated
  tariff estimate when the cost text is absent), rechecks the request ID and row price before clicking, and marks
  the typed request downloaded only after a non-empty document has been persisted. A zero-cost non-receipt is blocked for
  review. `sister query ipotecaria-persona-fisica` still starts only the free preview and returns its flow ID. An observed
  homonym page confirms *Accetta*, then an admin chooses a candidate and continues without repeating the paid click.
  Legacy download paths remain blocked for priced rows, except that the explicitly free receipt can be saved. Stale
  intended or clicked actions become `uncertain` after five minutes when status is read, blocking retry pending
  reconciliation. Confirmed *Salva* recovery requires a persisted document ID and links it to the exact request. Executor
  coverage is synthetic; no live paid click has been made.
- **Phase 4:** the AdE ledger migration is integrated into `DATABASE_REVISION` and the isolated migration chain. The
  statement, statement-detail, and receipt parsers; file-import commands; separate euro ledger; evidence checks for
  manual allocations; and the `/admin/wallets` panel are implemented. A receipt allocation with a recognized cost label
  now creates one idempotently linked `mortgage_search_charge` row. Statement parsing has been checked against a saved
  AdE HTML page; receipt and detail parser tests still use synthetic text/HTML. No saved AdE detail, receipt, or Richieste
  page is available. The saved statement page is imported into the local ledger; no portal fetch or receipt/detail import
  has been performed.
  The ledger does not write to SISTER's app-credit wallet. Receipt allocations remain explicit admin work; known labels
  project typed charges linked to the allocation, while unknown labels stay allocated but unclassified. Statement-detail
  allocations do not create cost rows because their current parser does not establish a charge kind and item quantity.
- **Phase 5:** cancellation-aware mortgage/pignoramento counts are attached to each inspection, and `/persons` now has
  a typed-table live-burdens filter backed by `database.subjects_with_live_burdens(kind=…)`. The helper and profile CTE
  merge repeated acts by office + RP + RG + year and treat an observed cancellation annotation as canceled across those
  repeated rows. The risk strip shows the latest dated transfer decree in each inspection. `/health` now reports an
  expired session as not auth-ready. Targeted regressions cover expired session readiness, typed projection, the
  live-burdens filter, and panel rendering. Template tests verify the `/persons` active-burdens card, metric link, and
  JavaScript column mapping, and render two normalized inspections together in one formalities panel. Typed-first and
  legacy JSON projection behavior are covered separately by the database-backed reader regression. A running `/persons`
  render is still pending; related note-extraction, health, and entity-template work is detailed under Phase 5 below.

Focused verification on 2026-11-16: the mortgage-flow, AdE ledger, browser-dispatch, parser, formalities-panel,
persons-index, service-worker, wallet, web-auth, CLI, client-contract, entity-template, query-form, parser-utility,
workflow, and MCP-wrapper tests plus the invalid-profile route guard passed **671 tests** with two optional
configuration/schema warnings. This run includes the anonymized formalità fixture, reusable live-burden query, MCP wrapper
regression, and the expanded database-backed two-inspection render through both typed-first and legacy JSON reads.
Coverage includes the rendered `/persons` live-burdens card/column, checkbox-to-register association guard and
mismatch-before-click case, saved AdE statement page shape, exact `idRichiesta` forwarding, refusal to start an
unconfirmed MCP preview, and the absence of any MCP paid-confirmation tool. An earlier isolated rerun of the
database-backed panel regression was skipped because the PostgreSQL fixture reported an `OperationalError`; it ran and
passed as part of this broader suite. The updated mortgage-search and persons-index test files pass local `ruff check`,
and `git diff --check` is clean. The MCP test fakes the SDK registration boundary;
the optional package is not installed here, so stdio transport and SDK serialization remain unchecked. The admin quote
panel has a direct template render test, and all paid-flow web routes are asserted admin-only. The latest full-suite
result and failure categories are recorded below; the repository suite is still not verified green. Paid-flow portal
behavior and running-service page renders remain unchecked.

Read-only Playwright checks on 2026-11-16: direct navigation to `/persons/MSSLNE82D48Z211J`,
`/persons/LRNBBR74A61Z149H`, and `/admin/wallets` redirected to `/auth/login` with the requested path in `next_url`; the
page title was `Accesso — SISTER`. The browser had no signed-in SISTER session, so the subject panels and wallet panel
were not rendered. No credentials were entered. This confirms the login gate only, not the page content or its rendering
on the running service. After the user reported an AdE-only sign-in, a read-only navigation to
`/Servizi/SceltaServizio.do?tipo=/T/TM/SUEC_` still ended at `/Servizi/login.jsp` (`Uscita`). Its page heading said
`Utente non abilitato, per accedere ai servizi occorre essere registrati.` The statement and date-selection form were
unavailable. The user reported this as an AdE-only signed-in session; the page indicates that this browser identity cannot
access the SISTER service, but does not identify which registration or entitlement step is missing. No portal form was
submitted.

Runtime probe on 2026-11-16: the local service at `127.0.0.1:8025` responds to `/health` with HTTP 200, but the
unauthenticated response exposes only `{"status":"degraded"}`. The route withholds detailed auth, database, and optional
component state from anonymous callers, so this response does not identify the cause. It is not evidence that the
authenticated SISTER pages are ready for a browser render.

Database verification on 2026-11-16: after aligning the new SQLModel definitions to their Alembic types, named foreign
keys and delete rules, and indexes, a scoped `compare_metadata` check across all 13 mortgage/AdE tables returned no
differences against the migrated local database. The standalone global `alembic check` also compares many pre-existing
legacy and externally managed tables outside this plan, so it is not a plan-scoped drift result.

Full verification refreshed on 2026-11-16 after the template-render regressions:
`.venv/bin/python -m pytest -p no:logfire -q --tb=short` completed with
**1,974 passed, 2 skipped, and 16 failed**. The failures are outside this paid-flow scope: one manual-valuation test has
no matching `property_asset` fixture; two OMI quote tests see the configured local DSN/data; three OMI cache tests do not
match current fixture behavior; six persona-giuridica homonym tests call a fake portal with an outdated signature; two
subject-access migration tests find an FK/model diff; and two web tests disagree with current 3D-geocoding and theme
language behavior. The earlier admin-activity test-double failure is now passing. The repository suite is not yet
verified green; these 16 failures remain untriaged.

New implementation findings (2026-11-16): the accept-result parser projects an inspection only when the portal result exposes both
an inspection number and date; otherwise no identity is fabricated. An observed homonym page is treated as confirmation
of *Accetta*, but the selected person is not inferred: the admin chooses one candidate, and the browser revalidates the
captured radio values and selected fiscal code before continuing. The homonym route records the selection page visit and
projects an inspection only when the resulting page can be parsed. It does not repeat the paid accept click. This behavior
has fake-page and route tests, but no live portal verification. Legacy archived payloads still lack `page_visits`; the
new homonym step records its own visit, while the complete page-by-page flow has not been validated. The anonymized
formalità fixture now covers visible-text parsing; there is still no anonymized raw HTML to validate DOM selectors.

The legacy `/admin/subject-formalities/download` route's invalid-profile guard referenced `HTTPException` without
importing it, which would have turned malformed profile input into a server error. The route now imports the exception
and a regression asserts HTTP 400 for an invalid profile; normal download behavior remains under the admin-session gate.

Paid-action recovery locks the flow/action rows and, on a status read, marks `intended` or `clicked` actions older than
300 seconds `uncertain`, retaining recovery evidence and blocking an automatic retry. This is a lazy status-triggered
recovery check, not a startup recovery worker. Note/title quotes now carry a short-lived token bound to the exact payable
formalità and portal checkbox values. The browser rechecks the captured selection values and visible RP/RG before
submitting. It now parses the current formalità page and requires each quoted RP/RG pair to match the formalità attached
to that exact checkbox value; a missing parser association or changed pair blocks submission. Afterward, it accepts the
action only when every requested row is uniquely matched by RP/RG in Richieste. A missing or ambiguous match leaves the
action uncertain. A synthetic fake-page regression covers both a matching checkbox/register pair and a mismatched pair
that must be rejected before the click; it passes in the 299-test focused run.
Titles remain one-at-a-time. CLI and MCP can request note/title quotes but cannot confirm them; their paid confirmation
routes require an admin web session.

The paid *Salva* path first reads the exact `idRichiesta` row without opening its document. Its quote requires an available
PDF request already linked to this inspection, uses a positive live row price when parseable, and falls back only to a
fresh dated tariff if the row has no cost text; a non-empty unparseable cost is blocked. A 0,00 € non-receipt row is not
treated as free. Before the click,
the downloader requires an allowlist containing only that ID, compares the live row cost text and amount with the quote,
and locates one matching *Salva* link. Confirmation also requires one non-empty downloaded file and a database document
ID; the typed request is then linked to that document. Manual reconciliation of an uncertain *Salva* action also requires
the admin to supply an existing, non-empty document ID before marking the request downloaded. CLI/MCP quote calls now
accept `portal_request_id` for this step, while confirmation remains web-admin-only.

The synthetic executor coverage does not establish that current AdE pages use these selectors, labels, row layouts, or
cost formats. A text-only anonymized formalità fixture derived from saved capture 8043 is now available, but there is no
anonymized raw HTML or Richieste capture and no live portal fetch or paid click has been exercised. A first authenticated
session can still stop before *Accetta* to verify authentication, form rendering, navigation, and capture behavior. The
statement parser has been checked against the saved statement page; statement-detail and receipt parsers still have only
synthetic coverage, and their identifiers are stored only when explicitly present.

The subject-page reader previously merged legacy payload amounts into the same `cost_summary` as typed amounts, even
though those payload fields do not record whether a value was a tariff estimate, a portal quote, or a posted debit.
That could present an uncharged price as spend. The reader now shows attributed spend only from typed charge rows backed
by a receipt or statement detail, labels it `Addebiti attribuiti`, and suppresses legacy amounts without charge
provenance. `cost_page` and `tariff` rows remain quote/estimate evidence and are not shown as posted spend. This does not
make the account statement a per-inspection record: the statement still proves the debit, while detail/receipt evidence
must link it to a specific inspection before it appears in that inspection's summary.

Receipt allocations now create a typed charge row only when the statement debit, receipt-line amount/date, explicit
inspection/request identity, and known cost label all match. Each charge links to its allocation and each receipt line
can be allocated once. Unrecognized labels remain in the euro ledger without a guessed charge kind; statement-detail
allocations also remain unprojected because their current parsed evidence does not establish item kind and quantity.
The migration `20261207_mortgage_charge_allocation` adds these database uniqueness and provenance links.

Review changes retained as design constraints: every *Salva* is potentially paid; clicks are recorded before execution
and uncertain outcomes block retry; the global API key cannot identify an admin; and ambiguous euro statement rows stay
unattributed until supporting evidence exists.

## Why

On 2026-11-11 and 2026-11-12 two ispezioni ipotecarie (T 91777 Palermo, T 79390 Ravenna) were run by hand through the
shared Chrome session, and their data was loaded with one-off scripts. That session showed five gaps:

1. **A paid click had no price check, and the app does not know when AdE charges.** *Visualizza note* on 12 notes queued
   12 PDFs listed at 4,00 € in Richieste, with no cost page. The account statement (see "Account statement") shows the charge
   is *per ogni nota visionata*: 8,00 € on 06/11 for the 16 notes requested that day, nothing on 12/11, and 32,00 € on
   13/11 (8 notes). So a note is charged when it is opened or saved, not when requested, and not every time.
2. **The portal's rules were found by failed attempts**: one titolo per request, only for notes flagged *Presenza titolo
   telematico*, PDF works where XML answers *Funzione non disponibile*.
3. **The persona-fisica inspection is not a query of the app.** `run_ispezione_ipotecaria` stops at the cost page; the
   omonimi page, the *Elenco sintetico delle formalità* and the Richieste downloads were done by hand.
4. **Formalità, annotations and costs live in a JSON payload** (`sister_archive.request_payload`, read by
   `database.get_subject_mortgage_formalities` with JSON paths), written by throwaway parsers.
5. **The euro spent at AdE was recorded by hand and wrongly at first.** The wallet `sister:ade:CNNMNL74L04G273K` was built
   from the Richieste prices and stood at −13,60 €; rebuilt on 2026-11-15 from the account statement it holds 4 top-ups and
   10 charges and matches the portal: 147,20 € in, 144,80 € out, 2,40 € left.

## Goals

- No request that AdE charges for leaves the app without the user seeing its price and what is already owned.
- A persona-fisica inspection runs as one resumable flow, stopping at every paid step.
- Formalità and their costs are typed rows that can be queried across subjects.
- Every posted AdE movement has a separate ledger entry backed by the account statement; receipts and *Dettaglio* support
  itemization and attribution, and any unexplained balance or charge remains visible.

## Non-goals

- Solving the CAPTCHA, or accepting a cost without a person confirming it (see "Decisions", D1).
- Changing the app-credit wallets, Stripe checkout or gift coupons.
- The `/documents/<id>` ispezione page: that is `docs/ispezione_template_plan.md`.
- Mortgage inspections by property, company or note. The design must not block them, but only persona fisica is built.

## What exists today (checked 2026-11-16)

| Piece | Where | State |
|---|---|---|
| Inspection automation | `utils.run_ispezione_ipotecaria`, `_handle_cost_confirmation` | fills the form, reads the cost, returns `confirmed: false` unless `auto_confirm` |
| Richieste list and download | `utils.run_consultazione_richieste`, `browser.download_richieste_documents` | lists rows with a *Salva* link; download is a separate action |
| Guarded paid flow | `mortgage_flow.py`, `browser.py`, admin routes and `mortgage_search_flow.html` | *Accetta*, note/title requests, and exact-ID paid *Salva* have flow executors; admin session only confirms/reconciles |
| Formalità reader | `database.get_subject_mortgage_formalities` | reads typed inspection/formalità rows first, falls back to archived formalità JSON, and shows spend only from receipt/detail-backed charge rows |
| Subject page panel | `templates/components/subject_formalities.html`, `web._merge_inspections_into_formalities` | table per inspection, attributed-charge line only when receipt/detail evidence links it to the inspection, pending-download notice, admin *Scarica da Richieste* |
| Note PDF parser | `utils._parse_ispezione_ipotecaria_pdf`, typed `mortgage_inspection*` tables | per note, from the PDF |
| Manual capture | `scripts/capture_page.py` | stores the current tab as a `manual_capture` request (read-only); `--parse` also parses formalità and checkbox values |
| Euro ledger | `ade_statement_import`, `ade_statement_row`, `ade_ledger_entry`, `ade_receipt*`, `ade_charge_allocation` | dedicated AdE ledger and admin panel implemented; one saved statement is imported locally, while receipt/detail evidence is still absent |
| Receipt | *RICEVUTA PER ISPEZIONI DEL <date>* in Richieste, 0,00 € | PDF with quietanza, prospetto di liquidazione and one line per cost type; read by hand once |

### Findings from stored payloads (checked 2026-11-16)

Before the migration below, the configured development `sister` database was at Alembic revision
`20261204_billing_transaction_self_reference`. On 2026-11-16, after verifying that target, `sister db migrate` applied
revisions `20261205_mortgage_search_tables` through `20261207_mortgage_charge_allocation`; `sister db status` then reported
`20261207_mortgage_charge_allocation` as head. The backfill was run only for successful inspection requests 8038 and 8044.
Read-back verified T 91777 (8038): 7 formalità, 0 annotations, 7 note requests and 1 title document; T 79390 (8044):
12 formalità, 9 annotations and 1 note request. `get_subject_mortgage_formalities` returned all 7 and 12 typed
formalities. The T 79390 typed quote selected 12, found document 2085 already owned, quoted 11 new notes at 44,00 € as a
dated tariff estimate, and returned 11 payable checkbox identities. At the time of that quote check, no AdE statement
or receipt had been imported; the later statement import is recorded in Phase 4. Its posted debits are now ledger
movements, but no cost has been attributed to an inspection. The production database was not changed.
Their archived JSON is not a uniform result envelope:

- inspection metadata is under `portal_capture.inspection`, formalità are a top-level `formalities` array, and later
  document activity is under `portal_capture.pdf_note_request` and `portal_capture.title_requests`;
- formalità include `documenti_correlati` annotation data and portal selection values, while note-document entries may
  carry `portal_request_id`, RP/RG, document ID, timestamp and filename;
- title data is represented as `single_request` for one request and as `single_attempts`/`xml_attempt` in another;
- neither payload has `page_visits`.

Projection must therefore normalize each observed envelope shape without treating attempted title requests as completed
requests or assuming that a document list covers every selected formalità. Keep the raw JSON fallback during rollout and
preserve page captures for the new flow, since the existing payloads cannot reconstruct the visited-page timeline.
Repeated projection is keyed by the archived request ID. Update the search/formalità rows in place once paid-action or
pending-request rows reference them; a wholesale child-row delete would erase workflow history.
The stored manual capture 8043 provides a real visible-text sample for parser regression. An anonymized, text-only fixture
derived from that sample is checked in at `tests/fixtures/mortgage/formalita_saved_capture_anonymized.json`; all 12 rows
and all 12 synthetic checkbox identities parsed, with 9 related annotations including 5 cancellations. Raw HTML and
original identifiers remain in the database capture and were not copied into the fixture. This does not verify current
portal DOM selectors.

The first paid action is *Accetta*, but `mortgage_search` describes only an inspection AdE has already accepted. A durable
pre-accept flow row is therefore needed to save the preview and consent before that click; paid actions belong to this flow
and gain a `search_id` only after the accepted result is recorded. Do not synthesize an inspection number for a preview.

The service API key is global and `require_api_key_dependency` does not identify a user or expose a role. It cannot prove
D1's admin requirement for a paid confirmation. Keep quote/status API-key accessible, but authorize confirm and reconcile
through the authenticated admin web session until the API has an identity-bound admin credential; CLI/MCP must not turn the
shared service key into user consent.

### Account statement (supplied 2026-11-15)

*Gestione Contabile → Estratto conto* (`/Servizi/SceltaServizio.do?tipo=/T/TM/SUEC_`), period 15/01–15/11/2026, convention
stipulated 06/04/2026. It is the authoritative list of what AdE charged: one row per day, cost type and area.

| Date | Row | Accredito | Addebito |
|---|---|---|---|
| 19/10 | Ricarica castelletto | 19,20 | |
| 06/11 | Per ogni nominativo e/o immobile richiesto | | 25,60 |
| 06/11 | Per ogni nominativo e/o immobile richiesto (Sicilia) | | 6,40 |
| 06/11 | Per ogni nota visionata | | 8,00 |
| 06/11 | Per ogni titolo visionato | | 8,00 |
| 06/11 | Per ogni titolo visionato (Sicilia) | | 16,00 |
| 06/11 | Ricarica castelletto | 48,00 | |
| 11/11 | Per ogni nominativo e/o immobile richiesto (Sicilia) | | 6,40 |
| 11/11 | Per ogni nota visionata (Sicilia) | | 28,00 |
| 11/11 | Per ogni titolo visionato (Sicilia) | | 8,00 |
| 11/11 | Ricarica castelletto | 40,00 | |
| 12/11 | Per ogni nominativo e/o immobile richiesto | | 6,40 |
| 12/11 | Ricarica castelletto | 40,00 | |
| 13/11 | Per ogni nota visionata | | 32,00 |
| | **Totale** | **147,20** | **144,80** |

Deposito castelletto on 15/11/2026: 2,40 €. Each row has a *Dettaglio* (`DettaglioEstrattoContoIpo.do`) that has not been
read yet; it should say which inspections and notes a row covers. A structural read of the saved statement HTML on
2026-11-16 found that detail is posted through one form to `/Servizi/GestCont/EstrattoConto/DettaglioEstrattoContoIpo.do`:
each movement is a radio input named `id`, alongside hidden `ufficio`, `chiaveConv`, period, year, and `profilo` fields.
No form was submitted and the hidden values were not copied into this plan. The follow-up read-only browser attempt on
2026-11-16 reached the AdE `Uscita` page with an explicit “Utente non abilitato” message, so statement-detail retrieval
still needs a browser identity enabled for the SISTER service before it can be verified or implemented against a live
page.

What it corrects: the Richieste *Costo* column is a price, not a charge; the 13/11 charge has no request of that day, so
it was triggered by opening or saving notes requested earlier (the app's *Scarica da Richieste* is the likely trigger and
must be treated as a paid action until the *Dettaglio* says otherwise). Which 8 of the 12 Ravenna notes were charged, and
why the other requests were not, is **not known**.

Facts measured in the session, to be encoded as constants with their date:

| Fact | Value | Source |
|---|---|---|
| Inspection, one office | 6,40 € | cost pages of T 91777 and T 79390; receipt of 11/11 |
| Inspection, national | 19,20 € | cost page of T 79048 (not accepted) |
| Note, when viewed | 4,00 € each | receipt of 11/11 (7 → 28,00 €); statement 06/11 (8,00), 13/11 (32,00) |
| Titolo | 8,00 € | fee notice and receipt of 11/11 |
| *Visualizza note*, PDF, n notes | n requests in Richieste, **no cost page, no charge that day** | 12/11 12:08:07; statement |
| *Visualizza titolo*, more than one note | "Non è possibile richiedere più di un titolo alla volta." | 12/11 |
| *Visualizza note/titolo*, XML | "Funzione non disponibile." | 11/11 and 12/11 |
| *Visualizza titolo* on a note without titolo telematico | back to *Scelta province*, nothing produced | 12/11, note 11 |

## Plan

Five phases. Each is one reviewable change with its own tests; phases 1 and 2 have no browser code and can ship first.

### Phase 1 — Typed storage for inspections and formalità

New tables (SQLModel in `db_models.py`, one Alembic revision, `DATABASE_REVISION` bumped in the same change):

| Table | Key columns | Notes |
|---|---|---|
| `mortgage_search` | `id`, `request_id` → `request`, `inspection_number`, `inspection_date`, `office`, `scope` (`office`/`national`), `subject_fiscal_code`, `requester_fiscal_code`, `motivation`, `convention_request_code`, `formalities_count`, `annotations_count` | one row per accepted inspection; unique (`inspection_number`, `inspection_date`) |
| `mortgage_search_flow` | `id`, `request_id` → `request`, `search_id` (null until accepted), `subject_fiscal_code`, `office`, `scope`, `state`, `current_step`, `state_payload`, `started_by`, `started_at`, `updated_at` | durable preview/run state; one row per flow request, retained if the flow stops or has an uncertain outcome |
| `mortgage_search_formality` | `id`, `search_id`, `ordinal`, `kind` (trascrizione/iscrizione/annotazione), `direction` (favore/contro), `formality_date`, `registro_particolare`, `registro_generale`, `public_official`, `repertory`, `repertory_date`, `act_description`, `location`, `electronic_note`, `electronic_title`, `portal_selection_value`, `note_document_id`, `title_document_id` | unique (`search_id`, `ordinal`) |
| `mortgage_search_annotation` | `id`, `formality_id`, `ordinal`, `number`, `annotation_date`, `description`, `is_cancellation` | the *Documenti successivi correlati* lines |
| `mortgage_search_request` | `id`, `search_id`, `formality_id`, `kind` (`note`/`title`), `format`, `portal_request_id` (`idRichiesta`), `status` (`intended`, `requested`, `available`, `downloaded`, `failed`, `uncertain`), `requested_at`, `document_id`, `paid_action_id` | one row per note or titolo asked for the flow's formalità; unique (`portal_request_id`) |
| `mortgage_search_charge` | `id`, `search_id`, `kind` (`inspection`/`note`/`title`), `quantity`, `unit_amount`, `amount`, `currency`, `charged_at`, `portal_request_id`, `basis` (`cost_page`/`receipt`/`statement_detail`/`tariff`), `receipt_id`, `wallet_tx_id` | one row per cost line **that the evidence ties to this inspection**; a charge that cannot be tied to an inspection is not stored here (see Phase 4) |

Names follow the existing `mortgage_inspection*` tables (which stay per-note, from the PDF) and the memory rule: no abstract
bases, discriminator columns.

Work:
- `result_parsers.parse_formalita_list(text, checkbox_values)` — the parser used on 2026-11-13 for the 12 Ravenna rows, moved
  from the scratch script. The text-only anonymized saved-capture fixture covers the real page's row and checkbox
  association shape without retaining source identities or property details.
- `database._project_mortgage_search(session, request_pk, data)` called from `_project_response` for
  `ipotecaria_persona_fisica`; idempotent by `request_id`, updating search/formalità rows in place so existing paid-action
  and pending-request history remains attached.
- `sister db backfill mortgage-searches` — projects requests 8038 and 8044 and any later one from their payloads.
- `get_subject_mortgage_formalities` reads the tables; the JSON reader stays as fallback for one release, then goes.
- `mortgage_search_request` makes a request that was accepted but not yet archived visible: the quote (Phase 2) counts a
  formalità with a request in `requested`, `available`, `uncertain` or `downloaded` as **owned or pending**, never as new,
  so it cannot be submitted a second time. Pending rows are listed on the panel with their `idRichiesta`.
- Cross-subject query helper: `database.subjects_with_live_burdens(kind=…)` (live = no cancellation annotation), used in phase 5.

Tests: anonymized saved-capture parser fixture; projection on
`fresh_db`; backfill twice gives the same rows and keeps paid-action history; migration down/up round-trip; typed reader
matches the existing subject-panel shape; before/after panel render comparison for both stored inspections.

### Phase 2 — Price check before a paid request (no browser code)

`sister/ade_tariff.py`: the measured prices above, each with `measured_on`, and

```python
quote_note_request(search_id, ordinals, fmt) -> Quote
quote_title_request(search_id, ordinal) -> Quote
```

`Quote` = lines (`kind`, `quantity`, `unit_amount`, `amount`), `already_owned` (formalità whose `note_document_id` /
`title_document_id` is set, with a same-office document for the same RP/RG **and year**, or with a
`mortgage_search_request` in a non-failed status), `new_cost`, `blocked` with the
reason (more than one titolo, no titolo telematico, XML). Ownership is matched on office + RP + RG + year, never RP alone.

UI on the formalità panel (admins): checkboxes per row, *Richiedi note PDF* and *Richiedi titolo* open a confirmation that
shows the selected and already-owned rows, defaults to the not-owned rows, and names the blocked rows. For a step with a
portal cost page, show that amount. For a step without one, show an **estimate** from the dated tariff and label its source
and measurement date; do not call it a guaranteed maximum. The same quote guards each potentially chargeable *Salva* in
Richieste until the account-statement *Dettaglio* establishes the trigger and amount. Paid note/title and *Salva*
confirmations now submit through the phase 3 flow, which persists intent before clicking and blocks retry after an
uncertain outcome.

Rules: CSP-safe (`data-click`, nonce), CSRF on the POST, admin-gated like *Scarica da Richieste*.

Tests: quotes for T 79390 with only document 2085 archived (expected: 12 selected, 1 owned, 44,00 € new) and with the twelve
06/11 notes linked (12 owned, 0,00 €); blocked cases; estimates display source and measurement date; stale tariff blocks a
note quote; template render.

### Phase 3 — The persona-fisica inspection as a resumable flow

A state machine in `sister/mortgage_flow.py`, driven by `browser.py`, one page lock per step:

| Step | Portal page | Paid | Stops for |
|---|---|---|---|
| `form` | *Ricerca persona fisica* (office set through `_set_office`; national only when asked) | no | — |
| `preview` | *Anteprima* | **yes** on Accetta | confirmation with the amount read from the page |
| `omonimi` | *Elenco Omonimi* | no | choice when more than one |
| `formalita` | *Elenco delle note* | no | stored and ownership-aware quote shown |
| `notes` | *Visualizza note*, PDF | **yes**, no cost page | the quoted items and admin confirmation |
| `title` | *Visualizza titolo*, one note | **yes** | the quoted item and admin confirmation |
| `richieste_list` | *Richieste* (read the list, update `mortgage_search_request` statuses) | no | — |
| `richieste_save` | *Salva* / *Apri* of one row of this flow, by `idRichiesta` | **yes, until proven otherwise** | the quote of that row; a row listed at 0,00 € (receipts) is the only exception |

- Each step saves its page (`page_visits`) and its result; the flow can be resumed from the last stored step while the
  portal still shows it, and otherwise reports where it stopped (the list is lost after *Visualizza note*: seen 12/11).
- **Paid actions are recorded before they are made.** Table `mortgage_paid_action` (`id`, `flow_id`, nullable `search_id`, `step`, `quote`
  with amount and items, `confirmed_by`, `confirmed_at`, `state`, `portal_request_ids`, `result`, `reconciled_at`):
  1. `intended` is committed (with the quote and the confirming admin) **before** the click;
  2. `clicked` right after it; note/title requests are confirmed only when their Richieste rows match the selected RP/RG
     pairs; *Salva* is confirmed only after a non-empty document is persisted and linked; *Accetta* requires a readable
     accepted result page;
  3. if the app stops, the page closes, the session ends or the effect cannot be read, the action stays `intended` or
     `clicked` and becomes `uncertain`. An `uncertain` action **blocks every retry of the same step** until it is
     reconciled: against the exact Richieste row and, where available, the statement; for *Salva*, also verify that the
     returned document is present and linked. Reconciliation sets `confirmed` or `not_executed` and is itself a recorded,
     admin-only operation.
  The single-use token only stops the *app* from reusing a consent; this table is what stops the *portal action* from being
  replayed. Resume continues from the last stored step and never re-clicks a step whose action is not `not_executed`.
- A paid step needs a single-use confirmation token bound to the quote (amount + items); a different amount on the page
  than in the quote aborts before the click. Same pattern as the MCP `preview_query` → `submit_query` tokens.
- A row is `requested` only when the portal shows it in Richieste (same rule as `_confirm_request_submitted`).
- **Interfaces.** `query_forms.py` describes single-step queries, so it cannot carry per-step confirmation. It gets one
  entry, `ipotecaria-persona-fisica`, for the **free part only** (form, preview, omonimi, formalità list, stopping *before*
  *Accetta*; `documents="none"`). Everything paid goes through a dedicated flow API, the only way to spend:

  | Operation | HTTP | Who | Effect |
  |---|---|---|---|
  | start | `POST /api/v1/mortgage-searches` | API key | runs the free steps, returns the search id and the preview amount |
  | status | `GET /api/v1/mortgage-searches/{id}` | API key | step, stored formalità, pending requests, uncertain actions |
  | quote | `POST /api/v1/mortgage-searches/{id}/quote` | API key | quotes *Accetta*, note/title requests, or one exact Richieste `idRichiesta`; returns a quote-bound token when confirmable |
  | confirm | `POST /admin/mortgage-searches/{id}/confirm` | **signed-in admin session** | spends once: token + the quote's amount; one confirmation per paid step |
  | reconcile | `POST /admin/mortgage-searches/{id}/actions/{action}/reconcile` | **signed-in admin session** | resolves an `uncertain` action |

  `VisuraClient`, CLI (`sister mortgage start|status|quote`; `--portal-request-id` quotes one *Salva* row; and the
  `sister query ipotecaria-persona-fisica` preview alias), and MCP preview tools call the API-key operations. Paid
  confirmation and reconciliation are exposed only to a
  signed-in admin session until an identity-bound admin API is available; the shared service key is never treated as the
  confirming user.
  There is no "accept all": a confirmation names one step.
- **Closing the old paths.** The paid paths that exist today are changed in the same phase, otherwise they bypass D1:
  - `auto_confirm` (`models.py:555,597`, `routes.py:539`, `client.py:308`, CLI `--yes` at `cli.py:596,916,972`,
    `workflows.py:196` with `include_paid_steps`) is **ignored for ipotecaria**: the route answers *preview only* with the
    quote and an error naming the confirm operation; `--yes` keeps its meaning for re-downloading an answered query only.
  - `/api/v1/visura/ipotecaria` (`routes.py:516`) becomes a preview-only alias of *start*.
  - `download_richieste_documents` is split: **list** (free) and **save**. The existing callers get the quote guard:
    `routes.py:503` (`download-documents`: saves only rows already owned or at 0,00 €, lists the others), `web.py:4005`
    (the PDF-pair batch job: the same rule), `web.py:6554` (*Scarica da Richieste*: the admin confirmation of Phase 2).
    Until the statement *Dettaglio* shows what triggers a charge, **every *Salva* of a priced row is treated as paid.**
  - Workflows keep their paid steps off for ipotecaria until they call the flow API.
- `docs/query_forms.md`, `docs/mcp_server.md` and a new `docs/mortgage_search_workflow.dot/.svg` are updated.
- `scripts/capture_page.py --parse` runs the phase 1 parser on the currently displayed page's visible text and checkbox
  values; the option is read-only and does not navigate or click.

This phase touches `utils.py` browser flows, which cannot be exercised against current portal markup without a live
session: unit tests use synthetic HTML and fake page objects. The first real run is `--stop-before-paid` on a subject
already inspected, and must be recorded here before the phase is called done.

### Phase 4 — Statement, receipts and the euro ledger

Implementation note (2026-11-16): migrations `20261206_ade_statement_ledger` and `20261207_mortgage_charge_allocation`
are included in `DATABASE_REVISION`, `alembic/env.py`, and the isolated test migration chain. Together they create
`ade_statement_import`, `ade_statement_row`, `ade_ledger_entry`, `ade_receipt`, `ade_receipt_line`, and
`ade_charge_allocation`, and add the allocation link to `mortgage_search_charge`. This integration
does not mean the production database has been migrated; the configured local development database is at
`20261207_mortgage_charge_allocation`. The saved page `data/ade/boresello_2026.html.htm` was parsed and imported into that
local database on 2026-11-16. The import added 14 statement rows and rebuilt 14 ledger movements (4 top-ups, 10 charges),
with totals of 147,20 € in, 144,80 € out, and a 2,40 € balance. Repeating the import found the same source hash, added
zero rows, and returned the same balance. A read-only database audit confirmed one import, 14 statement rows, 14 ledger
rows and zero allocations. A fresh aggregate read-only audit on 2026-11-16 reconfirmed the row counts, the matching
147,20 € / 144,80 € / 2,40 € credit, debit, and balance totals, and a 0,00 € difference from the statement. Production
was not touched. Every AdE movement remains separate from SISTER's app-credit wallets.

- `sister/ade_statement.py` parses saved statement HTML by labelled columns and the row radio key. It handles the saved
  page's `Dal giorno … al giorno …` period, `Descrizione`/`Data operazione` columns, zero-valued empty amount columns,
  `TOTALE` row, and dated closing-balance label. It checks the period, totals, and closing balance; the real saved page
  parsed to 14 rows and the figures above. A synthetic regression test covers this page shape and rejects a mismatched
  total. The parser also reads saved *Dettaglio* HTML for explicit inspection numbers, `idRichiesta` values, and RP/RG
  pairs. The row key is the idempotency key. It does not fetch the portal. No saved *Dettaglio* page is present.

- `sister/ade_receipt.py` parses receipt text after PDF extraction (`pdftotext -layout`), including the date, payment
  method, line items, total, and identifiers that are explicitly printed. It rejects a mismatched total. No anonymized
  receipt or Richieste page is present; the parser has synthetic input only.
- File commands are available: `sister ade statement import <html>`, `sister ade statement detail-import <key> <html>`,
  `sister ade receipt import <pdf-or-text>`, and `sister documents import-receipts <file-or-directory>`. Receipt import
  reads local files; there is no portal fetch/download implementation in this phase.
- `sister/ade_ledger.py` rebuilds one `topup` or `usage` entry per statement row, idempotently, with a running balance.
  Because the statement supplies dates without times, credits are ordered before debits on the same day. A top-up can be
  linked manually to one `pagopa_payment_history` row only after exact amount, EUR currency, validated status, and date
  checks. No AdE amount is credited to the app-credit wallet.
- Admin allocations are manual and evidence-checked. Receipt allocations require an exact receipt-line amount and date
  plus an explicit matching inspection or request identifier. Statement-detail allocations require an inspection,
  request, or RP/RG identifier that matches stored typed data. The single-inspection rule requires exactly one inspection
  that day and an existing matching typed charge. A recognized receipt-line label creates a `mortgage_search_charge`
  linked to that allocation; the unique allocation link prevents duplicate projection, and a receipt line cannot be
  allocated twice. Unknown labels remain ledger allocations without a typed cost row. Statement-detail allocations do
  not create charge rows because the parsed evidence does not yet itemize cost kind and quantity. Ambiguous debit
  remainder stays visible as unattributed.
- `/admin/wallets` has a separate "Conto AdE (euro)" panel with totals, reconstructed balance, statement-balance
  difference, unattributed amount, movements, imports, receipts, and an admin allocation form. If the ledger tables are
  unavailable, the page shows a warning and leaves app-credit wallet rendering available.

Regression tests cover the 14-row statement totals (147,20 / 144,80 / 2,40), rejection of inconsistent totals, receipt-line
reconciliation, import idempotency, same-day ordering, migration round-trip, explicit-evidence allocation,
validated-pagoPA linking, and admin rendering. In addition, the statement parser was checked against the saved AdE HTML
and the resulting statement was imported and re-read through the idempotent CLI path. No saved receipt or *Dettaglio*
page is available, so the supplied receipt has not been reconciled against real statement details. No portal fetch has
been performed.

### Phase 5 — Subject page and housekeeping

- Risk strip from typed data (live mortgages, pignoramenti, latest decreto di trasferimento) for every inspection, not only
  the most recent; a filter on `/persons` for subjects with live burdens (new column in the `p` CTE, per the CLAUDE.md
  checklist: `SUBJECT_*_FIELDS`, `_profile_from_row`, JS `defs`, CSV). The filter deduplicates repeated rows by office + RP
  + RG + year and excludes an act when a matching typed row carries a cancellation annotation.
- **On `main`, not here (D5):** consolidation left from 2026-11-13: one file list (the category archive) with the hub showing counts; parcel cards link
  to the formalità row instead of repeating the extraction; ownership acts and formalità on one timeline with a source badge.
- Note extraction details on the subject and parcel views now use `get_document_structured_extraction`, the same normalized
  extraction source as `/documents/<id>`; the subject summary formats sections A–D and parsed property/party data.
- `/health` now reports an expired session as not auth-ready, using `VisuraService._session_lost_reason`.
- Entity-template renders without a `request` context are guarded in the template; the regression tests cover that
  render path without fabricating a request object.
- The duplicate `manual_capture` rows (8041, 8043) have not been deleted. They require a verified target database and
  confirmation that the inspections have been projected. A read-only check of the local database on 2026-11-16 found
  both are successful `manual_capture` requests with no `mortgage_search` row and zero typed formalities. The deletion
  precondition is not met; preserve both rows.

## Order, size and risk

| Phase | Depends on | Size | Main risk | How it is contained |
|---|---|---|---|---|
| 1 Storage | — | M | drift between JSON and tables | golden render test; JSON fallback for one release |
| 2 Price check | 1 | S | wrong "already owned" | match on office + RP + RG + year; shows the file it relies on |
| 3 Flow | 1, 2 | L | a paid click without consent; a replayed click after a crash; a bypass through an old path; portal layout changes | token bound to the quote; paid action recorded before the click and `uncertain` blocks retries; old paths closed in the same change; amount re-read before the click; saved-page tests; first run stops before paying |
| 4 Ledger | 1 | M | double entries or unsupported attribution | statement-row idempotency; the statement is authoritative for posted charges, while receipts and *Dettaglio* support itemisation and attribution |
| 5 Page | 1 | M | regressions on a busy template | render tests; Playwright check of two subjects |

Every phase with a page change is checked on the running service (`http://localhost:8025`) through the Playwright MCP
browser, signed in by hand: `/persons/MSSLNE82D48Z211J`, `/persons/LRNBBR74A61Z149H` and `/admin/wallets` are the reference
pages, and the checks made are listed in the change description.

Phases 1, 2 and 4 need no SISTER session. Phase 3 needs one real session to verify, and that run costs nothing if it stops
before *Accetta*.

## Decisions

- **D1 — Who may confirm a paid step?** *Decided 2026-11-15*: users with the admin role only (`SISTER_ADMIN_USERS`), one
  confirmation per paid step, no "accept all".
- **D2 — Tariff source.** The cost page is authoritative when present and a mismatch aborts before its confirmation click.
  For note steps with no cost page, show the dated tariff as an estimate and fail closed once it is stale. *Decided
  2026-11-15*: a measured rate is fresh for 30 calendar days. A receipt or
  statement updates estimates for later quotes and reconciles past actions, but cannot change or abort an action after it
  was charged. Alternative: an admin-editable tariff table with the same
  source, measurement date and audit history.
- **D3 — Opening balance of the AdE account.** *Resolved 2026-11-16*: the saved statement covers the convention from
  its start and reports a closing balance of 2,40 €. The imported dedicated ledger reconciles 147,20 € of credits less
  144,80 € of charges to the same 2,40 € balance.
- **D4 — Which inspection each statement row belongs to.** The historical reconstruction found 06/11 rows (25,60 + 6,40
  inspections, 8,00 notes, 24,00 titoli) and 13/11 notes (32,00, possibly T 79390 by inference only) without supporting
  links to a stored inspection. The read-only ledger audit found zero allocations: all 10 imported debit movements remain
  unattributed until each row's *Dettaglio* provides evidence; none is assigned to an inspection by date or inference.
  Importing those details still requires saved portal pages and a later read-only verification session.
- **D5 — Scope of phase 5.** *Decided 2026-11-15*: the page consolidation is done on `main`, not in this branch. This
  branch keeps only the phase 5 items that depend on the typed tables (risk strip, live-burdens filter).
- **D6 — Identity for paid confirmation.** *Resolved 2026-11-15*: the shared API key has no user identity or role. Confirm
  and reconcile require a signed-in admin web session; API-key clients can start, inspect and quote but cannot consent.

## Out of this branch, noted for later

- Inspections by property, company and note on the same flow.
- Notes in *Documenti per particella* once parsed (needs the note PDF parser to give parcels reliably).
- Showing the cost of a query before running it for all `documents != none` queries, not only ispezioni.
