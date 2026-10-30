# SISTER — Audit part 2: demo user and the document-purchase workflow

| | |
|---|---|
| **Date** | 2026-10-29 (21:30–21:45 CET) |
| **Target** | `http://localhost:8025`, commit `31c0e66` plus the uncommitted `entity.html` and `main.py` edits present at the time |
| **Accounts** | `demo@aecs4u.com` (role `user`, signed in through the real login form in a separate browser context) and the admin account already in the Playwright profile |
| **Method** | Playwright MCP + request-level probes from both contexts; code read of `sister/web.py`, `sister/database.py`, `templates/admin/document_purchases.html`; DB reads with `psql`; read-only review of the purchase/billing code in `../real-estates` and `../opendata` |
| **Part 1** | [`ui_audit_2026-10-29.md`](ui_audit_2026-10-29.md) (design/quality sweep as admin). Evidence shots for this part: `docs/ui_audit_2026-10-29/demo_*` and `admin_*` (git-ignored; they contain real data) |

**Severity:** 🔴 Critical · 🟠 High · 🟡 Medium · 🔵 Low.

> **Remediation (same day, after this report):** most findings were implemented; see **§6** for the status of each ID, what was verified live and what is still open. The text of §1–§5 describes the state *before* the fixes.

Sections 0 and 5 also preserve interim checkpoints from the audit evening. Where those checkpoints conflict with §6, §6 is the later status.

---

## 0. What was and was not done

**Done**
- Demo login (failed first attempt because of the cookie banner, see DU9), what the demo user can reach before any purchase, route-by-route.
- The purchase workflow end to end for the *happy path*: catalogue → "Gestisci accesso" → create → demo user gains access → per-file access checks. Three keys for the same user were compared.
- Code review of the workflow and comparison with the purchase/billing implementations in `real-estates` and `opendata` (§4).

**Not done: the web UI went down at 21:40**
The uncommitted edit to `sister/main.py` imports `create_report_sharing_router` from `aecs4u_email`; the installed package does not export it. The `ImportError` is caught by the surrounding `try/except ImportError`, so **the whole web router is silently not mounted**: every UI route (`/`, `/forms`, `/admin/*`, `/documents/*`) returns `404 {"detail":"Not Found"}` while `/health` still answers 200 (finding **OPS1**). I did not touch your edit. The first negative-test run hit that 404 (nothing was created; confirmed in the DB). **At about 21:46 the UI came back and I ran the blocked tests in a second round** (validation, update, revoke with the real button, audit log page, the demo user's write attempts); their results are in §3 and the checklist in §5 is mostly ticked.

**Still not done:** the full design probe (contrast, overflow, tap targets, mobile) repeated as the demo user; the demo user renders the same templates as admin, so I only checked data and gating. The purchase pages at 375 px were also not re-measured.

**Cleanup.** Test data was created for document 1886 (the demo user's `username:`, `email:` and `id:` grants, then `audit.*@example.invalid` users). Every row was removed with scoped `DELETE`s limited to my own `AUDIT`/`audit.*` rows; `document_purchase` is back to **0 rows**. The activity log keeps the create/update/revoke events (about 25 lines), and a demo browser context was opened and closed.

**Credential handling.** The demo account was located in the plaintext `../local_users.txt` file. A command also printed its contents in the audit output; no credential values are reproduced here. The follow-up changed SISTER to use the hashed file and restricted the plaintext files to mode `600` (§6). On 2026-10-30, at the user's request, the active `demo@aecs4u.com` hash was set from the existing credential in `local_users copy.txt` and verified without displaying it. This establishes the requested file value, but does not establish that the credential changed from its previous value; rotation remains unconfirmed.

---

## 1. Summary

| # | Sev | Finding | Ref |
|---|---|---|---|
| 1 | 🔴 | **Purchase gating only protects the `documents` table.** The same extracted data (names, codici fiscali, owners, properties) is readable by any signed-in demo user without a purchase through `/results`, `/results/<id>`, `/api/ui/visura/<id>` and `/dossiers/*` (list, view, download). | DU1 |
| 2 | 🟠 | **Grants by `email:` or `username:` are accepted, reported as success, and do nothing.** Only `id:<user.id>` matches, and `user.id` for local users is the e-mail address, which no screen shows. | PW1 |
| 3 | 🟠 | The sidebar is shown to every role (14 links for the demo user); you asked for admin-only. Hiding it is cosmetic unless the routes are also gated (see DU1, DU4). | DU3 |
| 4 | 🟠 | Non-admin users can open the **Query** page (`/forms`); I did not submit one, but nothing visible stops a `user` role from sending paid portal queries. Decide and gate. | DU4 |
| 5 | 🟠 | The purchase record has **no price, payment reference, status, expiry or revocation trail**; `purchased_at` is never checked; revoke is a hard delete. **Decision: build purchases on `aecs4u-billing`**, which is already in `uv.lock` but not imported anywhere in `sister/`; target design in §4a. | PW2, §4, §4a |
| 6 | 🟠 | **Confirmed live:** clicking "Revoca" shows no dialog (CSP: "Executing inline event handler violates … script-src") and access is revoked immediately. | PW4 |
| 6b | 🟠 | A document id above `bigint` returns **HTTP 500**; validation errors expose raw Python text (`invalid literal for int() with base 10: 'abc'`, `Invalid isoformat string: 'banana'`) in English. Future-dated purchases are accepted. | PW3 |
| 6c | 🟡 | The audit log shows "Revoca accesso · Completata · HTTP 303" but **not which user or document**, so grants cannot be audited from it. | PW6 |
| 7 | 🟠 | **A broken import silently removes the whole web UI** while `/health` stays green. | OPS1 |
| 8 | 🟡 | No self-service: a user with 0 purchases sees "0 documenti acquistati disponibili" and no way to ask for access (`/contact` is 404). | DU5 |
| 9 | 🟡 | Cookie banner has no dialog semantics, steals the first click on the login button and leaves keyboard focus behind it. | DU9 |

---

## 2. Demo user (role `user`)

### 🔴 DU1 — Purchase gating does not cover results, dossiers or the results API
Signed in as `demo@aecs4u.com`, with **no** purchase:

| Request | Result |
|---|---|
| `/documents` | **0** documents ("0 documenti acquistati disponibili", tabs 0/0/0): gating works |
| `/documents/1886/view`, `/download`, `/pdf`, `/plan`, `/geojson`, `/parcel-map.json` | 404 |
| `/admin`, `/admin/documents`, `/admin/activity`, `/admin/document-purchases`, `/browser` | 403 (styled HTML, 12 KB) |
| `/persons/<cf>`, `/companies/<piva>` | 404 (shows the search page) |
| **`/results`** | **200**, "Mostrate 1–50 di **4012** risultati", codici fiscali in the page |
| **`/results/<id>`** | **200**, 1–2 codici fiscali per request |
| **`/api/ui/visura/<id>`** | **200 `application/json`, 3.4–5.7 KB, with owner (intestatari) data** |
| **`/dossiers`** → `/dossiers/view/*.json`, `/dossiers/*.json?download=1` | **200**, 13 files listed, viewable and downloadable |

So the purchase model (commit `31c0e66`, "purchase-based document access") protects the stored `.p7m`/PDF files and the persons/companies archive, but the *content of every request ever run* is open to any logged-in user. If purchases are meant to be the data boundary, this defeats them.
**Fix.** Treat `results`, `api/ui/visura/*`, `dossiers/*` and `workflows/*` like documents: admin-only, or filter by the requests belonging to documents the user bought (join request id → document). Add a test per route with a non-admin principal (the existing `tests/test_web.py` additions only cover the document routes).

### 🔵 DU2 — Error responses are inconsistent
Unpurchased document: `/documents/1886/*` → `404 application/json {"detail":"Document not found"}` (31 B); `/documents/900/view` → `404 text/html` (12 KB error page); `/documents/900` → **`200`** "Result 900: Request not found for ID 900" (the results viewer's soft 404, English, title `SISTER - Result 900`). The path fallback `/documents/{path:path}` turns unknown ids into a 200. Return one styled 404 for browsers and JSON for API clients, and never 200 for "not found".

### 🟠 DU3 — Sidebar visibility (your requirement: admin-only)
At audit time the demo user saw the full left sidebar: *Dashboard, Query, Risultati, Workflow OpenData, Documenti, I miei preferiti, Dossier* and the *Informazioni* group (Guida, Cheat sheet, Glossario, Metodo di valutazione, Informazioni, Documentazione API, Privacy). Only *Amministrazione* and *Browser* were admin-only. `[shot: demo_d1440_light_after_login]`
The initial implementation question was whether non-admins should retain a smaller navigation. The follow-up uses a top bar for non-admins (Documenti, Preferiti, Metodo di valutazione, Guida), hides the sidebar and hamburger, and gates the routes separately; see §6 DU1 and DU3.

### 🟠 DU4 — What may a `user` do on the portal?
At audit time, a `user` could open `/forms` (all 18 query types, Multi-step, Batch upload) and `/workflows`; `GET /api/ui/batch` answered 405, indicating a POST handler. I did **not** submit a query, so acceptance and cost were not tested. The later product decision enables paid queries for signed-in users; the implementation and its unverified runtime limits are recorded in §6 DU4 and §7.
Related: `/browser/status` returns 200 JSON (mode, state, queue) to the demo user while `/browser` is 403.

### 🟡 DU5 — Empty state of Documenti
With zero purchases the page shows the document-type catalogue and "0 documenti acquistati disponibili", the tabs all say 0, but there is no sentence on how to obtain access and no contact (`/contact`, `/help`, `/terms` are all 404; part 1 UX2). Add "Non hai ancora documenti. Contatta l'amministratore / Richiedi accesso" with a working action. After a purchase the counter reads "1 documenti acquistati" (plural).

### 🟡 DU6 — Session cookie
`aecs4u_session`: `HttpOnly`, `SameSite=Lax`, 24 h, **not `Secure`** (acceptable on `http://localhost`; make sure production sets it). Consent cookie 1 year. No cookie is issued on a failed login.

### 🔵 DU7 — Profile
`/profile` for the demo user: Username and Email `demo@aecs4u.com`, **Name "—"**, Role `user`, English labels and a `Logout` button. It does not show the **account key** that purchases are matched on (PW1).

### 🟡 DU8 — After purchase (works, with notes)
With the `id:` grant: `/documents` lists 1 document (tabs 1/1/1); `/documents/1886/view` 200 (`application/pkcs7-mime`, 18.6 KB, `inline`), `/download` 200 (`attachment`), `/pdf` 200 (79 KB, **5.7 s** to typeset), the detail page 200 (219 KB, 5.3 s, and it never reached network idle within 30 s because of the parcel-map polling), `/plan` and `/geojson` 422 (not a planimetria, correct), `/documents/1885/view` still 404 (isolation between documents holds). The detail page rendered for the demo user once the `entity.html` edit was in the working tree (part 1 S1: it was 500 before).

### 🟠 DU9 — Cookie banner on first visit
In a clean browser context the banner (`#cookieConsent`) has no `role="dialog"`, no `aria-modal`, no accessible name, and keyboard focus stays on the password field behind the overlay (`document.activeElement` = `#password`). Mouse clicks on **Accedi** are intercepted by the overlay (my automated click timed out for 30 s); pressing Enter in the password field still logs in. Make it a real modal (focus into it, trap, restore) or a non-blocking bottom bar.

---

## 3. Purchase workflow (admin)

### How it works (from code and the DB)
```
/admin/documents  → "Gestisci accesso" → /admin/document-purchases?document_id=<id>
form: user key (id:/email:/username:…) · document id · "Acquistato il" · nota
POST /admin/document-purchases/{create,update,delete}   (admin only, CSRF, @_audited)
table sister.document_purchase(user_id text, document_id bigint FK, purchased_at, created_by, note, updated_at)  PK(user_id, document_id)
access: _require_document_access → has_document_purchase(user_key, id)  (admins bypass)
list:   _filter_purchased_documents(user, docs)
```
`user_key` is `_favorite_user_key(user)`: `id:<user.id>` if the principal has an id, otherwise `username:`/`email:` (`web.py:1698`).

### Verified
- Catalogue (1,070 documents, "N utenti con accesso" column) → "Gestisci accesso" opens the purchase page with the document id pre-filled (the user field is empty and nothing has focus).
- Create through the real form: `303 → ?status=created`, green "Acquisto registrato.", counter "Acquisti registrati 1", row shows `#1886 · VISURA FG. 357 PART. 262 SUB. 702 DI MILANO`, file name, type, **"registrato da id:admin@aecs4u.com"**. `purchased_at` empty → "now", shown in Rome time at **minute** precision (`2026-10-29T21:37`).
- CSRF enforced: a `POST` without the token → `403 {"detail":"CSRF token missing or invalid"}` (a JSON body instead of the styled error page).
- Admin-only: demo user gets 403 on every `/admin*` GET.
- Access takes effect immediately for view/download/pdf/page; other documents stay closed.

### 🟠 PW1 — Only `id:<user.id>` grants access; the other key forms silently do nothing
Same user, same document, three grants created through the form, each answered "Acquisto registrato.":

| User key typed | Matches the demo user? |
|---|---|
| `username:demo@aecs4u.com` | **no** (404 for the demo user) |
| `email:demo@aecs4u.com` | **no** |
| `id:demo@aecs4u.com` | **yes** |

The form text offers `id:123`, `email:nome@example.com` or `username:nome` as equally valid. For local-file users `user.id` is the e-mail, so even the `id:` form is guessable only if you know that; no screen shows the key (Profile lists username/email/role). The table has no user FK, so any string is stored (`id:audit.tester@example.invalid` would have been accepted).
**Fix.** (a) resolve the typed user against real accounts (autocomplete / "Utente" select, store the canonical key) and reject unknown keys; (b) show "Chiave account: id:…" on `/profile`; (c) or normalise lookups so that all three key forms of one principal match. Add a test: grant by each key form, assert access.

### 🟠 PW2 — The record cannot represent a purchase
Columns: `user_id, document_id, purchased_at, created_by, note, updated_at`. Missing: price/currency, payment or invoice reference, source (admin grant vs. paid), status, expiry, revocation time/actor/reason. `purchased_at` is **never read** for access (`has_document_purchase` only checks existence), so a purchase dated 2099 grants access now; "Revoca" is a hard `DELETE` so history lives only in the activity log (and `/admin/activity` lists the last 100 events of the `admin.document_purchase.` prefix). See §4 for what the sibling projects store.

### 🟡 PW3 — Validation and messages
From the code (`web.py:3363–3432`, `database.py:2655–2715`): bad key, non-positive id, bad date and notes over 2,000 characters raise `ValueError` and come back as a red alert via `?error=…`; unknown document and duplicate (user, document) are rejected in code (good, no silent overwrite). Issues:
- the messages are **English** in an Italian page (`The user key must be 320 characters or fewer`, `Document not found`, `This user already owns this document`);
- the error text travels in the URL (`?error=…`) and is rendered in the alert, so anyone can craft a link that makes an admin page show an arbitrary red banner (content spoofing; Jinja escapes it, so no XSS). Use a flash message or an error code;
- the duplicate check is read-then-insert; a double click can still end in a primary-key `IntegrityError` → 500. Use `INSERT … ON CONFLICT DO NOTHING` and report the conflict;
- `update` re-posts the minute-precision local time of the input, so saving a note silently rewrites the purchase time to `:00` seconds and passes through a local→UTC conversion;
- a date typed as `2099-…` is accepted.
**Live results (second round, admin):**

| Input | Result |
|---|---|
| key `demo`, `id:`, `phone:123` | 303 → red alert in English "Use a user key such as id:123 or email:name@example.com" |
| document `abc` | 303 → **"invalid literal for int() with base 10: 'abc'"** (raw Python message) |
| document `0` | 303 → "Document ID must be positive" |
| document `99999999999999999999` | **500 Internal Server Error** (reproduced twice; the value overflows `bigint`: bound the id to the Postgres range and catch `DBAPIError`) |
| document `99999999`, `2000000` | 303 → "Document not found" (correct) |
| date `banana` | 303 → **"Invalid isoformat string: 'banana'"** |
| note of 2,001 characters | 303 → "The note must be 2000 characters or fewer" |
| no CSRF token | 403 `{"detail":"CSRF token missing or invalid"}` |
| duplicate (user, document) | first 303 created, second 303 → "This user already owns this document" (correct, no overwrite) |
| `ID:Audit.Upper@Example.invalid` | **created as `id:Audit.Upper@Example.invalid`**: the prefix is lower-cased but the identifier is not, so `id:` keys are case-sensitive and `id:Demo@…` ≠ `id:demo@…` |
| purchase date `2099-01-01T00:00` | **accepted** and stored |
| note `<img src=x onerror=alert(1)>` | stored, rendered escaped in the list (no XSS) |
| `?error=Sessione scaduta: reinserisci la password` | rendered as a red alert on the admin page (content spoofing) |
| update: note only | 303 `status=updated`; time kept (the form re-posts the minute-precision value) |
| update to an existing (user, doc) / to a missing document / of a vanished row | 303 with "This user already owns this document" / "Document not found" / "Purchase record not found" |
| delete of a vanished row | 303 `status=not_found` → "L'acquisto era già stato rimosso" |

Two requests (`document 99999999` on the first run, `update` of a vanished row) were answered with `ECONNRESET`; neither reproduced on retry and both coincided with a `--reload`, so I do not count them.

### 🟠 PW4 — Revoke has no working confirmation
`document_purchases.html:108`: `<form … onsubmit="return confirm('Revocare l’accesso a questo documento?')">`. **Verified in the browser:** with a dialog listener attached, clicking "Revoca" raised **no dialog**, the console logged `Executing inline event handler violates the following Content Security Policy directive 'script-src 'self' 'nonce-…' blob:'`, the page went to `?status=deleted` ("Accesso revocato.") and the row was gone. The project rule is already "templates must not use inline event handlers" (CLAUDE.md); this template, added in the same commit, breaks it. Part 1's probe reported 0 inline handlers on this page only because the list was empty.
Replace with `data-confirm="Revocare…"` handled by `sister_actions.js`, or a Bootstrap modal. Revoking an access should also ask for a reason (and record it, PW2).

### 🟡 PW5 — UX of the admin pages
- The user field is a free-text input with no list of users; the document is a bare number ("Trova nel catalogo" link).
- After create/update/delete the success alert is `role="status"` but focus is not moved to it; the page keeps its scroll position at the top, so with 500 rows the new row is not visible.
- One row = two table rows (edit form + file/“registrato da” line + Revoca) inside a single `td colspan=5`; at 375 px the 5 inputs wrap in an order that separates the user from its Salva button. Not verified live (UI down).
- The list is capped at 500 with a text note; search matches user, id or file name but there is no filter by document type or date, and no pagination.
- The catalogue shows "0" for documents without grants: the column header is "utenti con accesso" in code but only the digit is visible; add a label/tooltip.

### 🔵 PW6 — Audit trail
Each action is wrapped in `@_audited("admin.document_purchase.<verb>")` and appears on `/admin/activity` (columns *Data e ora, Operatore, Operazione, Esito, Dettagli*), newest first, with actor `admin@aecs4u.com` and operations "Modifica acquisto", "Revoca accesso". The **Dettagli column only says "HTTP 303"** or "Richiesta non eseguita": it does not name the user key or document, the old values on update, or the validation error, and a rejected create and a successful one both end in HTTP 303 (the "Esito" word is the only difference). Add `user_id`, `document_id`, `source`, `amount` and the reason to the event payload and render them; otherwise the log cannot answer "who gave access to what, and why".

**Write attempts by the demo user (all correct):** `POST /admin/document-purchases/create` without CSRF → 403 `CSRF token missing or invalid`; with its own token → 403 `Admin role required`; `…/delete` with its token → 403 `Admin role required`; `GET /admin/activity` → 403; `PUT /documents/1885/favorite` on a document it does not own → 404 `Document not found`. The 403 body for these API-style calls is JSON, while a browser GET gets the 12 KB HTML page.

---

## 4. What `real-estates` and `opendata` do (reference implementations)

I read the code only; I did not run either service.

| Concern | **SISTER now** | **real-estates** (`routers/payments.py`, `services/credit_service.py`) | **opendata** (`routers/billing.py`, `templates/dashboard/buy_credits.html`) |
|---|---|---|---|
| Payment | none; admin types a row | Stripe **subscriptions** (`/payments/create-checkout`, `/success`, `/cancel`, customer portal) and a **credit wallet** | Stripe Checkout **credit packs** (`/billing/checkout/credits`) and subscriptions (`/billing/subscription`) |
| Price | none | `CREDIT_COSTS` per item type (`document_extraction` 10, `due_diligence` 20…), starter credits 50 | packages in the template (price in cents → credits + bonus) |
| Entitlement | join table `document_purchase` | derived from the **ledger**: `has_credit_purchase(user, item_type, item_id)` looks for a debit with `reference_id = "{item_type}_{item_id}"` | credit balance only (no per-item entitlement) |
| Ledger | none | `aecs4u_billing.WalletService`: `credit/debit`, `balance_after`, locks, daily limit, `DuplicateTransactionError`, `idempotency_key`; legacy `credit_accounts` migrated | same wallet (`wallet_credits.add_credits`) plus a local `Transaction` row with the Stripe ids |
| Spending flow | n/a | check "already purchased" → balance check → `spend_credits` → on failure `add_credits` **refund** (`_sales_extraction.py:150–190`); insufficient balance → **HTTP 402** with the cost | n/a |
| Webhook | n/a | signature verified; every handler wrapped, **errors swallowed and 200 returned** | signature verified; credit purchase handled inline |
| Admin | CRUD rows, activity log | `admin_grant` transaction type; security events `billing_checkout_created`, `billing_webhook_processed` | `/billing/admin/metrics`, admin user page shows purchases count |
| User UI | none (a count and an empty state) | pricing/services pages; the **Land Registry page has the Purchase button disabled "Coming soon"** | balance card, three packs, FAQ (expiry 12 months, non-refundable) |

**Initial recommendations (audit-time; implementation status is in §6)**
1. Use `aecs4u-billing`, already in SISTER's `uv.lock` (installed 1.2.0) but not yet imported from `sister/` at the time of this review. Both sibling projects already used it.
2. Keep SISTER's `document_purchase` table as the fast **entitlement** (better than real-estates' scan of the last 200 ledger rows) and add: `price_cents`, `currency`, `source` (`admin_grant|stripe|wallet`), `payment_ref` (unique), `wallet_tx_id`, `expires_at`, `revoked_at/by/reason`, instead of hard-deleting.
3. The *item type + item id* reference convention and the **402 + refund-on-failure** flow for every paid action (a document download and, per DU4, a portal query).
4. Security-event names for the purchase lifecycle (`purchase_created`, `purchase_revoked`, …) next to the activity log.
5. A user-facing "Acquista / Richiedi" action on the empty `/documenti` state and on each document, using the same flow.

### 4a. Initial integration sketch for `aecs4u-billing` (superseded by §6)

Read from the installed package (`aecs4u_billing` 1.2.0); not yet run in SISTER.

| Need | Package piece | Notes |
|---|---|---|
| Mount checkout, portal, invoices, credits, subscription views | `commerce.setup.setup_commerce(app, billing_config=…, theme=…, get_customer_id=…, prefix="/commerce")` | documented as "the recommended integration for real-estates, aecs4u-website, opendata, crowdaction"; views extend the app theme (SISTER already uses `aecs4u-theme`); includes rate limiting and security headers (check they do not clash with SISTER's own CSP/rate-limit middleware) |
| Start a purchase | `POST /commerce/purchase` with `price_id`, `mode="payment"`, `item_type`, `reference_id`, `idempotency_key` | the **price lives in Stripe**; the client sends only a price id, identity and e-mail come from the signed-in user, and `site_id` cannot be spoofed. This removes the price-tampering hole seen in opendata |
| Fulfil after payment | `@on_stripe_event("checkout.session.completed")` in `webhooks/registry.py`; replay protection in `webhooks/dedup.py` | handler reads `metadata.item_type == "document"` and `metadata.reference_id == <document_id>` and calls SISTER's `create_document_purchase(..., source="stripe", payment_ref=<session id>)`. Return 5xx on failure so Stripe retries |
| Credits instead of cards | `wallet.WalletService`: `credit/debit(..., idempotency_key=…)`, `TransactionType.{TOPUP,USAGE,REFUND,ADJUSTMENT,BONUS,EXPIRY}`, `wallet.stripe_topup.create_topup_checkout_session` | pay-per-document = `debit(reference_id="document_<id>", idempotency_key=…)` then insert the entitlement; failure after debit → `REFUND` |
| Admin grants | `TransactionType.ADJUSTMENT`/`BONUS` plus an entitlement row with `source="admin_grant"` | replaces today's free-text form; the admin picks a user, a document and a reason |
| Catalogue and prices | `products.service` (Stripe products/prices, `lookup_key`) | one price per document type (visura attuale/storica/soggetto, planimetria…) keyed by `lookup_key`; the UI shows the price before purchase |
| Invoices / refunds | `invoicing`, `payments`, `ledger` modules | needed for Italian invoicing; out of scope for a first cut |

**Integration points and checks in SISTER**
1. **One identity.** Billing resolves a `user_id` through its own dependency (`get_user_id`); SISTER's entitlement key is `_favorite_user_key(user)` (`id:<user.id>`). Make the billing resolver return the same value, and store only that canonical key (fixes PW1).
2. **Route gating first** (DU1/DU4): put `/commerce/*` (and `/forms`, `/results`, `/dossiers`) behind explicit role/entitlement checks before opening checkout to non-admins.
3. **Webhook route** must be exempt from SISTER's CSRF and cookie-auth middleware but signature-verified (`STRIPE_WEBHOOK_SECRET`); `.env.example` needs the Stripe keys, `site_id`, price lookup keys.
4. **Admin pages** keep `document_purchases.html`, but the create form becomes "Concedi accesso" (user picker + document + motivo) and the list shows source, amount, payment ref, status and a revoke that sets `revoked_at` (soft) and asks for a reason (PW4).
5. **Database:** new Alembic revision for the extra columns (add it together with the model change, per `CLAUDE.md`), and billing's own tables via its migrations/storage (`sqlmodel_storage`).
6. **Tests:** webhook replay (same `event.id` twice → one entitlement), unsigned webhook → 400, client cannot choose price, refund path, entitlement per key form, non-admin cannot reach `/admin/document-purchases/*`.
At audit time, the payment method and whether portal queries would be sold were unresolved. The follow-up implemented document purchases with credits and added an optional Stripe Checkout path; the Stripe path has not been exercised against a real account. The later decision enables paid portal queries using per-command prices from OpenData, with a shared fallback for batch rows and workflow steps (§6 DU4). This path is code-tested but has not been run against a live OpenData catalogue or wallet.

**Do not copy (problems I saw there)**
- opendata `CreditPurchaseCheckout` takes **`amount` and `credits` from the client** and uses `amount` as Stripe `unit_amount`, plus client-supplied `success_url`/`cancel_url`: a user can post a 1-cent price for 10,000 credits. The server must map a package id to price and credits.
- opendata credits the wallet with `reference_id=payment_intent` but **no `idempotency_key`**; the wallet de-duplicates on the key only, so a re-delivered webhook can credit twice.
- real-estates `stripe_webhook` catches every handler error and still returns 200 with the comment "Stripe will retry"; it will **not** retry, so a failed entitlement is lost. Return 5xx for transient failures and log the event id.
- real-estates `has_credit_purchase` reads only the latest 200 transactions (`limit=200`): an old purchase stops counting after 200 newer transactions. Also check-then-debit has no lock/idempotency key, so a double click can charge twice.
- `buy_credits.html` uses `onclick="buyCredits(...)"`, which SISTER's CSP forbids.

---

## 5. Operations finding and retest checklist

### 🟠 OPS1 — A failed import silently drops the whole web UI
`main.py` wraps the router registration in `try: … except ImportError`. With the current edit (`from aecs4u_email import create_report_sharing_router`, not available in the installed `aecs4u_email`) `Web UI inizializzata` is never reached and **every UI route is 404**, but the process runs, `/health` is 200 (`"degraded"`) and uvicorn `--reload` does not complain. Nobody is told. Let that `except` re-raise (or at least log at ERROR and make `/health` fail), and add a test that the app exposes `/` and `/forms` (401/302 when signed out).
**Resolution after the audit (see §6, OPS1):** the web-router import now stops startup instead of silently dropping every UI route, and the optional `aecs4u_email` import is guarded separately.

### Interim checklist checkpoint (about 21:46, before §6 remediation)
At this checkpoint, items 1 (create negatives), 2 (update), 3 (revoke; the confirm dialog did **not** appear), 4 (demo write attempts), and 5 (audit log: actor and operation only) had been run. Gating, sidebar, the demo-user design probe, and retesting after fixes were still outstanding. OPS1 had recovered and the UI was back at about 21:46. This checkpoint is superseded by §6.

Original list:
1. **Create (negative):** key `demo`, `id:`, `phone:1`; document `abc`, `0`, `99999999999999999999`, `99999999`; date `banana`; note of 2,001 characters; duplicate user+document; note `<img src=x onerror=alert(1)>`; expect an Italian error each, no row created, nothing executed.
2. **Update:** change only the note (check the time is not rewritten), move to an existing (user, doc) pair (expect error), update a vanished row.
3. **Revoke:** click "Revoca" and confirm whether a dialog appears (PW4); then confirm the demo user loses `/view`, `/download`, `/pdf`, the list entry and the detail page; revoke twice (expect "già rimosso").
4. **Permissions:** as the demo user, `POST /admin/document-purchases/create` with and without the demo's own CSRF token (expect 403 both times) and `PUT /documents/<id>/favorite` on a non-owned document (expect 404).
5. **Audit:** `/admin/activity` shows actor, user key and document id for create, update and delete (PW6).
6. **Gating (DU1):** after the fix, `/results`, `/api/ui/visura/<id>`, `/dossiers/*` as demo → 403/404.
7. **Sidebar (DU3):** demo has no sidebar or hamburger at 1440 and 375 px; admin keeps it; reload keeps both.
8. Re-run the contrast/overflow probe as the demo user on `/`, `/documents`, `/documents/<id>`, `/favorites`, `/profile`, `/settings`.

---

## 6. Remediation status after the audit (2026-10-29)

**How verified:** *Live* means headless Chromium checks in `scripts/ui_audit/` (`sweep.py`, `interactions.py`, `demo_purchases.py`) using the admin and demo sessions, plus earlier Playwright MCP checks. Automated verification used `tests/test_document_purchases.py` (35 collected cases, isolated PostgreSQL schema) and the full suite (**1,439 passed, 2 skipped, 0 failed, 0 errors**). Live test purchase rows were removed (`document_purchase` returned to 0 rows); the billing schema revision was applied to the live database.

| ID | Status | What changed |
|---|---|---|
| **DU1** data gating | ✅ live + code | Results, dossiers, workflow history, browser controls, and operational APIs require an admin. Non-admin polling through `/api/ui/visura/<token>` accepts only the signed token returned to that same user after a paid query; a raw or another user's request id returns 404. Query submission routes are separately billed under DU4. The demo user's tested admin-route denials remain 403. |
| **DU2** error shape | ✅ code | an unknown `/documents/<id>` is a 404 (was a 200 "not found" page); browsers get the themed page, API clients JSON (the earlier "inconsistency" was partly my probe sending no `Accept` header) |
| **DU3** sidebar admin-only | ✅ live | `_base.html`: sidebar, hamburger and `sidebar-visible` only for admins; other roles get a top bar (Documenti, Preferiti, Metodo di valutazione, Guida); `/` redirects them to `/documents` |
| **DU4** paid queries | ✅ implemented in code; live billing not tested | Signed-in non-admins may use `/forms`, supported single-query proxy routes, `/api/ui/batch`, and `/api/ui/workflow/stream`. Singles use an active OpenData price for the matching `POST /visura/...` command. Batch rows use that command price, falling back to the shared query product; workflow steps use the shared product. Prices prefer `customer_credits`, otherwise convert the OpenData EUR amount at 100 credits per euro. Missing prices/catalogue, wallet, or signing secret fail closed. Charges are reserved before work; rejected batch rows and incomplete workflow steps are refunded. Result polling requires a user-bound signed token. Focused tests cover catalog parsing, price fallback, wallet debit/refund, authentication policy, and token privacy; no live paid query or live OpenData price lookup was run. |
| **DU5** empty state | ✅ live | a notice above the tabs explains how to get access (link to the contact page and to the profile with the account keys); the counter reads "1 documento disponibile" (singular); the per-category message no longer says "indicizzato" |
| **DU7** profile | ✅ live | Italian, shows **Chiavi account** (`id:`, `username:`, `email:`) to give the admin |
| **DU8** after purchase | ✅ live | purchased document: page 200 (212 KB), download 200, revoke → 404; the counter is singular; status badges Italian (*Nessuna corrispondenza*, *Annullata*). The detail page took **9.8 s** to render for the demo user; see the residual performance observation in §7. |
| **DU9** cookie banner | ✅ live | `sister_enhance.js` gives the banner `role=dialog`, `aria-modal=true`, `aria-labelledby`, moves focus into it, traps Tab and restores focus; verified on a clean context (dialog attributes, focus inside, Tab trapped, closing releases the page) |
| **PW1** key forms | ✅ live + tests | access matches any key form of the principal (`id:`, `username:`, `email:`), case-insensitively; the form offers a list of known accounts (`datalist`); verified live: a grant typed `email:Demo@AECS4U.com` was stored as `email:demo@aecs4u.com` and gave the demo user access |
| **PW2** record model and payments | ✅ Implemented; real Stripe payment remains untested | Entitlements now record source, amount, currency, payment reference, wallet transaction, expiry, and revocation details. Revocation is soft; expired, future-dated, and revoked grants do not provide access. Signed purchase links and a PostgreSQL-backed credit wallet support document purchases. An optional Stripe Checkout path is implemented and covered by automated checks, but has not been exercised with Stripe. Details follow below. |
| **PW3** validation | ✅ live + tests | every refusal is a code with an Italian message (`?error=bad_document` …); non-numeric, ≤ 0 and > bigint ids → "ID documento non valido" (the 500 is gone); `banana` → "Data di acquisto non valida"; future dates and expiry before purchase refused; 2,001-character note refused; unknown document, duplicate, vanished row all give their message; an arbitrary `?error=` text is no longer rendered (checked: not shown); `id:` identifiers keep their case but matching is case-insensitive |
| **PW4** revoke confirm | ✅ live | `data-confirm` handled in `sister_actions.js`; the dialog "Revocare l’accesso a questo documento?" appears, **dismissing does nothing**, accepting revokes with the typed reason; no inline handler left on the page |
| **PW5** admin UX | ✅ live | focus moves to the result message after each action (set after `load`, because the theme resets focus at parse time), state filter, user list, numeric `inputmode`, expiry field, nested `<main>` removed. **At 375 px** the manager overflowed by 239 px (visually-hidden labels escaping the table scroller); fixed, now 0 px |
| **PW6** audit trail | ✅ live | `@_audited` merges `request.state.audit_params`; `/admin/activity` now shows the user key, the document (linked), the reason/note, old values on update and the refusal reason ("Rifiutata: data futura", …); the operation `admin.document_purchase.revoke` replaces `delete` in the log (the `/delete` URL stays as an alias that also revokes) |
| **OPS1** silent UI loss | ✅ live | an `ImportError` while mounting the web UI now stops startup (`SISTER_ALLOW_NO_WEB_UI=true` restores the old behaviour); the optional `aecs4u_email` import is guarded on its own; `/health` is minimal for anonymous callers (detail with the API key or as admin) and answers `HEAD` |

**PW2 implementation details.** The admin document page issues a signed `/buy/<token>` link that expires after 30 days by default. The purchase page requires login, identifies the document, and shows the configured card price or credit cost and balance. It does not list other customers' documents to non-admins.

With credits enabled, admins grant credits through an audited route and one wallet is used per person across identity-key forms. A buyer is charged only if they do not already own the document. The debit uses an idempotency key; if entitlement creation fails, the debit is refunded. Balances are stored in SISTER's PostgreSQL by default and were verified across process restarts. `SISTER_WALLET_STORAGE` selects the backend; volatile memory storage is for development only.

When Stripe settings are present, `sister/purchases_billing.py` mounts the signed `/webhooks/stripe` route and creates Checkout sessions server-side using the configured price id. The webhook verifies signatures, deduplicates events, and grants access once per payment reference. Automated tests cover replay, unpaid or unrelated sessions, already-owned documents, and disabled configuration. A real Stripe test payment was not run.

**Also changed (from Part 1):** `/terms`, `/help`, `/contact`, and `/notifications` exist; nested `<main>` landmarks were removed from six templates; dark-mode and contrast overrides were added; Query forms use examples as placeholders and validate codice fiscale / partita IVA before submission; the results table shows status and date beside the request id and uses an Italian pager; `LOCAL_USERS_FILE` points at the hashed users file, and the plaintext files are mode `600`; geocoding results are cached on disk and can be disabled. The hashes were checked against the existing passwords; this does not establish that the credentials were rotated.

**Tests.** The earlier recorded run collected 35 cases from `tests/test_document_purchases.py` and the user reported the full suite as **1,439 passed, 2 skipped, 0 failed, 0 errors**. The isolated PostgreSQL schema applies migrations through `20261119_billing_wallet_tables`. The paid-query follow-up added focused catalog, pricing, debit/refund, auth, and token tests; its focused result is noted in §8. The full suite was not rerun in that follow-up. The five failures reported earlier were fixed at their cause (see the 10-09 report's §1d: valuation-page text, KPI note ordering for F/D categories, the `request_type` fixture, and CLI exit codes for `error`, `expired`, and `needs_human`). **Timeline check:** this migration ID is dated later than the report's 2026-10-29 audit date; confirm whether the revision ID or the recorded date needs correction.

**Also fixed on the way (from the second audit)**
- *Manual valuations / OMI guesses were never saved*: the `ProgrammingError` warning on every person/company page was a real bug (`:legacy_zone_code IS NOT NULL` — PostgreSQL cannot infer the parameter type); fixed with a cast and reproduced before/after.
- *The same person could hold two grants under different key forms*: now refused (`email:a@b`, `id:A@B`, `username:a@b` are one identity).
- *The language switcher that only translated the user menu* is removed (the UI is Italian-only, as decided on 10-09).
- *Documents page*: only the active view is rendered (5.5 MB / 58,019 nodes → 2.4 MB / ~27,700 nodes by default).
- *Settings page* now gets SISTER's CSS and accessibility scripts.
- *Local users*: `.env` uses the hashed users file; the plain-text files are mode 600.
- *Plaintext password policy*: local authentication now refuses un-hashed passwords at startup by default; `ALLOW_PLAINTEXT_USERS=true` is a documented development-only opt-in.
- *Optional data failures*: manual-valuation and adjacent-parcel failures show an unavailable notice and are counted by component in privileged health detail; counters are process-local.

## 7. Remaining work and verification limits

1. **Run a Stripe test-mode payment.** Configure a Stripe test product and price, set `STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET`, `SISTER_DOCUMENT_PRICE_ID`, `ALLOWED_STRIPE_PRICE_IDS` and `SISTER_DOCUMENT_PRICE_CENTS`, then point the test webhook at `/webhooks/stripe`. Verify checkout completion grants access once, webhook replay has no effect, and a second payment for an already-owned document is surfaced for refund. Automated checks cover the local fulfilment path; no real Stripe transaction was run.
2. **Demo credential reset — file updated 2026-10-30.** At the user's request, the active local-user file's `demo@aecs4u.com` entry was set to a salted PBKDF2-SHA256 hash of the existing credential in `local_users copy.txt`. The hash was checked against the source without displaying either value; other entries, owner, and mode `600` were preserved. A live sign-in with the source credential succeeded. Because the credential may already have been the same and the running process was not restarted, this does not prove that a password rotation occurred or that the process loaded the new hash. The shared virtual environment was not changed.
3. **Paid query runtime verification.** The product decision is settled and the source path is implemented, but a live OpenData `GET /api/pricing`, per-command charge, batch fallback/refund, workflow settlement, low-balance response, and cross-user result-poll denial still need a runtime test. Configure active OpenData prices for each SISTER `POST /visura/...` command and the shared `/visura/query` fallback first. Per-user quotas beyond wallet balance and the 100-row batch cap are not implemented.
4. **Demo-user responsive/accessibility sweep — partial.** After signing in with the credential from `local_users copy.txt`, `/documents` showed 0 documents available. The exact `/documents/1886?map=45.47198,9.22834,17&basemap=streets&layers=c-sheets,c-parcels` URL returned the themed 404 for this account. This is consistent with purchase-gated access; without an entitlement, the favorite control on that detail page cannot be checked. I did not create a grant or bypass the gate. On pages the account could access, a live browser probe found 0 text-contrast failures in dark mode on `/documents`, `/favorites`, and `/settings`; it also found 0 horizontal overflow at 375 px on dark `/settings` and light `/documents`. The full route/theme/interaction sweep and hit-area measurements remain open; the 24 px CSS rules are otherwise only source-verified. The checked-in full sweep was not rerun after its earlier Chrome sandbox launch failure; these MCP checks cover selected pages only. The self-hosted share handler's network behavior was not checked in a browser.

**Residual performance observation.** Property-document first views measured 0.5–1.4 s (previously about 10 s), with repeat views at 0.0–0.1 s. Across eight person/company subjects, first views measured 0.6–5.5 s and repeats 0.4–3.3 s; the slowest sampled person archive was 1.6 MB and took 5.5 s cold / 3.1 s repeated. Further reduction would require geometry and valuation query work, which was not part of this pass.

**Contrast note.** Some intentionally coloured document chips and satellite-map labels remain bright in dark mode but measured readable (about 10:1); disabled controls were excluded from the contrast check.

## 8. Implementation update (2026-10-30)

The current source tightens image CSP by route, defers the Google 3D request until an explicit click, raises several hit areas to 24 px, adds a labeled favorite action to document-detail templates, and self-hosts the document share/email handler. This pass also removes the stale CSS rule hiding implemented `/notifications` and `/settings` links and translates visible result-detail controls and empty states into Italian. It fails closed on plaintext local passwords unless the development-only opt-in is set, and reports optional-data failures through an inline notice and authenticated health detail. The service was not restarted; CSS and templates were served from the running local app during the later partial browser pass below. The favorite control remains unverified on `/documents/1886` because the signed-in demo account has no accessible documents and the route returns a themed 404. The share handler is present in source, but its network behavior was not checked in a browser. A real Stripe test-mode transaction and the report's migration-ID chronology note also remain open.

Focused verification passed: 20 tests for password policy, entity rendering, optional-data degradation, and health reporting; `node --check` and ten targeted JavaScript checksum/format assertions also pass. The user reported a full-suite result of **1,439 passed, 2 skipped, 0 failed**; it was not rerun in this implementation follow-up. Jinja parsing and `git diff --check` pass. The authenticated partial browser pass on 2026-10-30 measured zero dark-mode text-contrast failures on `/documents`, `/favorites`, and `/settings`, zero horizontal overflow at 375 px on dark `/settings` and light `/documents`, and confirmed the themed 404 on the requested document route. It did not cover every route or interactive state. The shared virtual environment was not updated.

**Paid-query implementation follow-up (2026-10-30).** The product decision is to enable paid queries for signed-in non-admins, using per-command prices from OpenData. SISTER reads active rows from `GET /api/pricing`, matches the command's `POST /visura/...` endpoint, and uses `customer_credits` when present (otherwise EUR `amount` × 100, consistent with OpenData's price editor). Batch rows use their command price or a shared `/visura/query` fallback; each planned workflow step uses that shared fallback. The page confirms the exact total and matched price path before submission; if price, path, or unit count changes before submit, SISTER rejects it and requires a new quote. SISTER debits the existing aecs4u-billing wallet up front, refunds rejected batch rows and unfinished workflow steps, and gives each submitter a signed result-poll token bound to their account. Missing prices/catalogue, wallet, or result-signing key fail closed; the catalogue is cached for 60 seconds. The focused billing/auth suite passed **63 tests** with one existing Logfire warning; after the final currency-filter change, both price-catalog tests passed. Ruff passed for the new billing helpers and auth tests; Python/JavaScript syntax checks and `git diff --check` passed. Ruff still reports nine unrelated findings in existing `web.py` sections. The full suite and live OpenData/paid-query path were not rerun. Do not set a fixed query-credit environment value; configure product prices in OpenData instead. The shared virtual environment was not updated.
