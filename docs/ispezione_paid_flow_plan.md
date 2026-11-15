# Ispezione ipotecaria: guarded paid flow, structured storage, euro ledger — development plan

Status: **plan for review, nothing implemented.** Branch `feature/ispezione-paid-flow` (from `main` at `97e69c0`).
Written 2026-11-15.

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
- Every euro charged by AdE is a ledger entry backed by a receipt, and the unexplained difference is visible.

## Non-goals

- Solving the CAPTCHA, or accepting a cost without a person confirming it (see "Decisions needed", D1).
- Changing the app-credit wallets, Stripe checkout or gift coupons.
- The `/documents/<id>` ispezione page: that is `docs/ispezione_template_plan.md`.
- Mortgage inspections by property, company or note. The design must not block them, but only persona fisica is built.

## What exists today (checked 2026-11-15)

| Piece | Where | State |
|---|---|---|
| Inspection automation | `utils.run_ispezione_ipotecaria`, `_handle_cost_confirmation` | fills the form, reads the cost, returns `confirmed: false` unless `auto_confirm` |
| Richieste list and download | `utils.run_consultazione_richieste`, `browser.download_richieste_documents` | lists rows with a *Salva* link; download is a separate action |
| Formalità reader | `database.get_subject_mortgage_formalities` | reads `portal_capture.inspection`, `formalities`, `portal_capture.costs`, `pdf_note_request`, `title_requests` from the payload |
| Subject page panel | `templates/components/subject_formalities.html`, `web._merge_inspections_into_formalities` | table per inspection, cost line, pending-download notice, admin *Scarica da Richieste* |
| Note PDF parser | `utils._parse_ispezione_ipotecaria_pdf`, typed `mortgage_inspection*` tables | per note, from the PDF |
| Manual capture | `scripts/capture_page.py` | stores the current tab as a `manual_capture` request (read-only) |
| Euro ledger | `billing_wallets` / `billing_transactions` (aecs4u-billing), `pagopa_payment_history` | one EUR wallet written by hand on 2026-11-13 |
| Receipt | *RICEVUTA PER ISPEZIONI DEL <date>* in Richieste, 0,00 € | PDF with quietanza, prospetto di liquidazione and one line per cost type; read by hand once |

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
read yet; it should say which inspections and notes a row covers.

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
| `mortgage_search_formality` | `id`, `search_id`, `ordinal`, `kind` (trascrizione/iscrizione/annotazione), `direction` (favore/contro), `formality_date`, `registro_particolare`, `registro_generale`, `public_official`, `repertory`, `repertory_date`, `act_description`, `location`, `electronic_note`, `electronic_title`, `portal_selection_value`, `note_document_id`, `title_document_id` | unique (`search_id`, `ordinal`) |
| `mortgage_search_annotation` | `id`, `formality_id`, `ordinal`, `number`, `annotation_date`, `description`, `is_cancellation` | the *Documenti successivi correlati* lines |
| `mortgage_search_charge` | `id`, `search_id`, `kind` (`inspection`/`note`/`title`), `quantity`, `unit_amount`, `amount`, `currency`, `charged_at`, `portal_request_id`, `basis` (`cost_page`/`richieste`/`receipt`/`tariff`), `receipt_id`, `wallet_tx_id` | one row per cost line; `basis='tariff'` marks an amount not read on the portal |

Names follow the existing `mortgage_inspection*` tables (which stay per-note, from the PDF) and the memory rule: no abstract
bases, discriminator columns.

Work:
- `result_parsers.parse_formalita_list(text, checkbox_values)` — the parser used on 2026-11-13 for the 12 Ravenna rows, moved
  from the scratch script, with the saved page as fixture (`tests/fixtures/single_step/ispezione_elenco_note.html`, anonymised).
- `database._project_mortgage_search(session, request_pk, data)` called from `_project_response` for
  `ipotecaria_persona_fisica`; idempotent (replace by `request_id`).
- `sister db backfill mortgage-searches` — projects requests 8038 and 8044 and any later one from their payloads.
- `get_subject_mortgage_formalities` reads the tables; the JSON reader stays as fallback for one release, then goes.
- Cross-subject query helper: `database.subjects_with_live_burdens(kind=…)` (live = no cancellation annotation), used in phase 5.

Tests: parser fixture; projection on `fresh_db`; backfill twice gives the same rows; the subject panel renders the same HTML
before and after for both stored inspections (golden comparison of the table).

### Phase 2 — Price check before a paid request (no browser code)

`sister/ade_tariff.py`: the measured prices above, each with `measured_on`, and

```python
quote_note_request(search_id, ordinals, fmt) -> Quote
quote_title_request(search_id, ordinal) -> Quote
```

`Quote` = lines (`kind`, `quantity`, `unit_amount`, `amount`), `already_owned` (formalità whose `note_document_id` /
`title_document_id` is set, or with a same-office document for the same RP/RG **and year**), `new_cost`, `blocked` with the
reason (more than one titolo, no titolo telematico, XML). Ownership is matched on office + RP + RG + year, never RP alone.

UI on the formalità panel (admins): checkboxes per row, *Richiedi note PDF* and *Richiedi titolo* open a confirmation that
shows "12 selected · 12 already archived · cost up to 0,00 €", defaults to the not-owned rows, and names the blocked rows.
The same quote guards *Scarica da Richieste*, because opening or saving a note is what AdE charges for; the quote says
"up to" until the charging rule is confirmed from the statement *Dettaglio*.
Until phase 3 exists the confirmation ends with "run this on the portal" plus the list; after phase 3 it submits.

Rules: CSP-safe (`data-click`, nonce), CSRF on the POST, admin-gated like *Scarica da Richieste*.

Tests: quotes for T 79390 with only document 2085 archived (expected: 12 selected, 1 owned, 44,00 € new) and with the twelve
06/11 notes linked (12 owned, 0,00 €); blocked cases; template render.

### Phase 3 — The persona-fisica inspection as a resumable flow

A state machine in `sister/mortgage_flow.py`, driven by `browser.py`, one page lock per step:

| Step | Portal page | Paid | Stops for |
|---|---|---|---|
| `form` | *Ricerca persona fisica* (office set through `_set_office`; national only when asked) | no | — |
| `preview` | *Anteprima* | **yes** on Accetta | confirmation with the amount read from the page |
| `omonimi` | *Elenco Omonimi* | no | choice when more than one |
| `formalita` | *Elenco delle note* | no | stored (phase 1), quote shown (phase 2) |
| `notes` | *Visualizza note*, PDF | **yes**, no cost page | the phase 2 confirmation; never without it |
| `title` | *Visualizza titolo*, one note | **yes** | the phase 2 confirmation |
| `richieste` | *Richieste* | no | downloads the rows of this flow by `idRichiesta`; free receipts too |

- Each step saves its page (`page_visits`) and its result; the flow can be resumed from the last stored step while the
  portal still shows it, and otherwise reports where it stopped (the list is lost after *Visualizza note*: seen 12/11).
- A paid step needs a single-use confirmation token bound to the quote (amount + items); a different amount on the page
  than in the quote aborts before the click. Same pattern as the MCP `preview_query` → `submit_query` tokens.
- A row is `requested` only when the portal shows it in Richieste (same rule as `_confirm_request_submitted`).
- `query_forms.py` gets the `ipotecaria-persona-fisica` entry with `documents="always"`; CLI, `/forms`, API and MCP follow
  from the table. `docs/query_forms.md` and a new `docs/mortgage_search_workflow.dot/.svg` are updated.
- `scripts/capture_page.py` stays as the manual fallback and gains `--parse` (runs the phase 1 parser on what it reads).

This phase touches `utils.py` browser flows, which cannot be exercised without a live session: unit tests use saved pages
with a fake page object (as for `_wait_for_search_form`); the first real run is `--stop-before-paid` on a subject already
inspected, and is recorded in this document before the phase is called done.

### Phase 4 — Statement, receipts and the euro ledger

- `sister/ade_statement.py`: parse the *Estratto conto consultazioni* page (rows, totals, deposit, period) and its
  *Dettaglio*; table `ade_statement_row` keyed by the row's radio value (`201~06/11/2026~RM~0~~`). The statement is the
  source of the ledger; receipts and Richieste only explain its rows. Fixture: the page of 15/11/2026, anonymised.
- A read-only portal step fetches the statement for a period (free, no CAPTCHA seen); `sister ade statement import <file>`
  loads a saved page.

- `sister/ade_receipt.py`: parse *RICEVUTA PER ISPEZIONI DEL <date>* (quietanza, prospetto di liquidazione, payment method,
  one line per `Elementi` × `Importo`). Fixture: the 11/11 receipt, anonymised. Table `ade_receipt` (+ `ade_receipt_line`).
- The Richieste step downloads free receipts; `sister documents import-receipts` handles files already on disk.
- Reconciliation: each receipt line is matched to `mortgage_search_charge` rows of that date by kind and quantity; a match
  sets `basis='receipt'` and `receipt_id`; what does not match is listed, never guessed.
- Ledger writer `sister/ade_ledger.py`: one `usage` transaction per statement charge and one `topup` per *Ricarica
  castelletto* (linked to its validated pagoPA row), idempotent by the statement row key, running balance recomputed in
  date order (the scripts of 2026-11-13 and 2026-11-15, made permanent). The statement gives days, not times: within a day
  top-ups are ordered first.
  The wallet user id stays `sister:ade:<codice fiscale>` so no sign-in identity can resolve to it.
- `/admin/wallets`: a separate "Conto AdE (euro)" section with top-ups, charges, receipts and the **unexplained
  difference**; app-credit wallets keep their list. Times shown in Europe/Rome (they are one hour early today).

Tests: statement parser (14 rows, 147,20 / 144,80 / 2,40); receipt parser; reconciliation of the 11/11 receipt (6,40 +
28,00 + 8,00 = 42,40 €) with the three 11/11 statement rows; ledger idempotency and ordering; a ledger total that differs
from the statement is reported, not hidden.

### Phase 5 — Subject page and housekeeping

- Risk strip from typed data (live mortgages, pignoramenti, latest decreto di trasferimento) for every inspection, not only
  the most recent; a filter on `/persons` for subjects with live burdens (new column in the `p` CTE, per the CLAUDE.md
  checklist: `SUBJECT_*_FIELDS`, `_profile_from_row`, JS `defs`, CSV).
- **On `main`, not here (D5):** consolidation left from 2026-11-13: one file list (the category archive) with the hub showing counts; parcel cards link
  to the formalità row instead of repeating the extraction; ownership acts and formalità on one timeline with a source badge.
- Note extraction summary on the subject page reads the same source as `/documents/<id>` (today it shows "0 immobili · 0
  soggetti" for notes whose page has sections A–D).
- `/health`: an expired session is not `auth_ready` (use `VisuraService._session_lost_reason`).
- `tests/test_entity_template.py`: pass `request` to the two failing renders.
- Delete the duplicate `manual_capture` rows (8041, 8043) after phase 1 has projected their inspection.

## Order, size and risk

| Phase | Depends on | Size | Main risk | How it is contained |
|---|---|---|---|---|
| 1 Storage | — | M | drift between JSON and tables | golden render test; JSON fallback for one release |
| 2 Price check | 1 | S | wrong "already owned" | match on office + RP + RG + year; shows the file it relies on |
| 3 Flow | 1, 2 | L | a paid click without consent; portal layout changes | token bound to the quote; amount re-read before the click; saved-page tests; first run stops before paying |
| 4 Ledger | 1 | M | double entries | idempotency keys; receipts as the only source of "confirmed" |
| 5 Page | 1 | M | regressions on a busy template | render tests; Playwright check of two subjects |

Every phase with a page change is checked on the running service (`http://localhost:8025`) through the Playwright MCP
browser, signed in by hand: `/persons/MSSLNE82D48Z211J`, `/persons/LRNBBR74A61Z149H` and `/admin/wallets` are the reference
pages, and the checks made are listed in the change description.

Phases 1, 2 and 4 need no SISTER session. Phase 3 needs one real session to verify, and that run costs nothing if it stops
before *Accetta*.

## Decisions needed before coding

- **D1 — Who may confirm a paid step?** *Decided 2026-11-15*: users with the admin role only (`SISTER_ADMIN_USERS`), one
  confirmation per paid step, no "accept all".
- **D2 — Tariff source.** Proposed: constants with their measurement date, overridden by what the cost page or a receipt
  says; a mismatch aborts the step. Alternative: a table editable in the admin area.
- **D3 — Opening balance of the AdE account.** *Resolved 2026-11-15*: the statement covers the convention from its start;
  the ledger was rebuilt from it and matches (2,40 €).
- **D4 — Which inspection each statement row belongs to.** The 06/11 rows (25,60 + 6,40 inspections, 8,00 notes, 24,00
  titoli) and the 13/11 notes (32,00, attributed to T 79390 by inference) are in the ledger without a link to a stored
  inspection. To be settled by reading each row's *Dettaglio* in the phase 3 verification session.
- **D5 — Scope of phase 5.** *Decided 2026-11-15*: the page consolidation is done on `main`, not in this branch. This
  branch keeps only the phase 5 items that depend on the typed tables (risk strip, live-burdens filter).

## Out of this branch, noted for later

- Inspections by property, company and note on the same flow.
- Notes in *Documenti per particella* once parsed (needs the note PDF parser to give parcels reliably).
- Showing the cost of a query before running it for all `documents != none` queries, not only ispezioni.
