# SISTER improvement proposals

_Review date: 2026-09-26. Scope: data retention, request execution, SISTER/CIE session handling, CAPTCHA-dependent document requests, workflow integration, and possible AI-assisted decisions._

This is a proposal backlog, not a statement that every behavior has been reproduced against the live SISTER portal. Findings based on local source are marked **Code finding**; portal behavior and external `aecs4u-auth` behavior need verification against the deployed versions before implementation. Links to `aecs4u-auth` use the sibling checkout at `../../aecs4u-auth`.

## Priorities at a glance

| Priority | Proposal | Recommended action | Effort |
|---|---|---|---|
| Done | Separate response retention from cache freshness (§1.1) | Cache freshness is independent; deletion is disabled by default and retention is configured in days | S |
| Done | Fix `/visura/intestati` argument mismatch (§1.2) | Remove the unsupported argument and keep regression coverage | S |
| Implemented, integration unverified | Reconcile workflow submission with its actual backend (§1.3) | Add an authenticated SISTER proxy to opendata and a client contract test; deployed end-to-end behavior remains to verify | S–M |
| Partial; deployment/retry policy open | Make request outcomes durable and explicit (§1.4, §4.1–4.2) | Persist lifecycle state, claim atomically with expiring worker leases, distinguish pre-handler recovery from unknown outcomes, and expose stable error categories; bounded retries and operator resume remain deferred | M–L |
| Partial; live verification open | Correct session readiness and recovery (§2) | Auth API and SISTER migration are implemented in the local worktrees; live portal checks and dependency release remain | M |
| Done in source; portal verification open | Make document requests opt-in and CAPTCHA outcomes honest (§3) | Keep unattended extraction separate from paid/document submission; preserve CAPTCHA work as `needs_human` | S–M |
| Partial | Improve batch checkpointing and workflow run UX (§4.3–4.4) | Persist custom workflows and lifecycle controls; harden batch outcome retries and verify deployed integration | S–M |
| P3 | Evaluate AI for bounded ambiguous decisions (§5) | Run an offline evaluation before any production integration | M |
| Done | Generic search dispatch, ispezione ipotecaria arguments, worker crash after auth timeout, SQLite connection leak (§1.5) | Fixed with regression tests; not yet committed | — |

Recommended sequence: **verified request-path fixes → request-state reliability → CAPTCHA/document flow → workflow and batch alignment → optional AI evaluation.** Durable lifecycle state now uses atomic claims and expiring leases; live portal checks and retry/idempotency policy remain open. Claims are safe across multiple workers, while expired jobs are recovered only before the recorded side-effect boundary.

## 1. Fix verified request-path and data issues

### 1.1 Response cleanup uses cache TTL as retention

**Initial code finding.** The original implementation called `cleanup_old_responses(RESPONSE_TTL_SECONDS)` every cleanup interval, coupling cache freshness to deletion of stored response rows ([services.py](../sister/services.py), [database.py](../sister/database.py)).

**Proposal.** Separate the two policies:

- Keep `RESPONSE_TTL_SECONDS` for deciding whether a cached response is fresh enough to reuse.
- Add an explicit response-retention setting, such as `RESPONSE_RETENTION_DAYS`, with a documented default that preserves results indefinitely (or disables deletion) unless an operator opts into a finite retention period.
- Make cleanup target only eligible response/cache records; never delete extracted domain data as a side effect of cache expiry.

**Implementation status (2026-09-26).** Cache freshness remains controlled by `RESPONSE_TTL_SECONDS`; database cleanup runs only when `RESPONSE_RETENTION_DAYS > 0`, in days. The default `0` disables deletion and is documented in [README.md](../README.md). Expired in-memory/cache entries do not delete persisted response rows, and cleanup logs the deletion count. Database tests cover disabled retention, finite retention, and preserving recent records.

**Acceptance criteria.** A response older than the cache TTL remains retrievable while within configured retention; cache lookup treats it as stale; disabling/omitting retention does not delete response history. Document the effect of finite retention and ensure cleanup is observable.

### 1.2 `/visura/intestati` passes an unsupported argument

**Code finding.** [`esegui_visura_intestati`](../sister/browser.py) passes `target_index=` to [`run_visura`](../sister/utils.py), whose current signature does not accept it. This raises `TypeError` on that call path.

**Proposal.** Decide whether `target_index` is part of the intended behavior. If it is, add it to the function contract and implement the selection; if it is not, remove the caller argument. Add a focused regression test for the intestati route through the browser handler.

**Acceptance criteria.** A representative intestati request reaches the intended result path without `TypeError`; tests cover both the parameter contract and the endpoint response.

**Regression test.** `tests/test_browser_dispatch.py::test_intestati_call_matches_run_visura_signature` now passes as a normal test.

### 1.3 Workflow submission points at a missing or mismatched route

**Code finding at review time.** The CLI client and form definitions submitted to `POST /visura/workflow` ([client.py](../sister/client.py), [form_config.py](../sister/form_config.py)); the SISTER API did not define that endpoint. The web application already contains workflow-run pages and proxies submission, status, cancel, and resume operations to the separate opendata workflow service ([web.py](../sister/web.py)). The stale CLI/forms path should be verified against the deployed opendata API before being called a failure in every environment.

**Proposal.** Make one workflow service the source of truth. Prefer updating CLI and form submission to use the supported opendata workflow API (or a SISTER proxy that forwards to it), including the same payload, authentication, error mapping, polling, cancel, and resume behavior. Avoid implementing a second workflow engine inside SISTER unless there is a concrete deployment requirement the opendata service cannot meet.

**Acceptance criteria.** Each preset/custom workflow submission path is exercised end to end; returned run IDs can be polled; backend errors are shown as errors rather than falling through to a generic visura route. Document which service owns workflow persistence and execution.

**Regression test.** `tests/test_client_contract.py::test_workflow_submission_reaches_a_workflow_route` checks that `VisuraClient` reaches the real FastAPI route; it stubs the opendata transport, so it does not prove deployed integration.

### 1.4 Requests can become unobservable

**Code finding.** SISTER uses an in-memory `asyncio.Queue` ([services.py](../sister/services.py)). It cannot restore queued work after restart, and callers need a durable status/result record to distinguish queued, running, failed, and missing requests.

The request path persists a job row and status before dispatching, and rejects submissions when the database is read-only so the API does not accept work it cannot durably record. Atomic conditional claims prevent two workers from running the same persisted job, and workers renew a 180-second lease every 30 seconds. Startup and periodic recovery leave live leases alone; expired jobs before `side_effect_started` are safely requeued, while expired jobs after it become `failed/interrupted_unknown` and are never replayed. The worker requires the side-effect marker to persist before invoking the browser handler. A previously stored response takes precedence over recovery. Handler crashes and auth timeouts produce queryable error outcomes, queue clear records cancellation, and no-match/CAPTCHA-required results have distinct statuses.

**Remaining work.** The in-memory queue is process-local, and there is no durable CAPTCHA human-resume workflow. The side-effect marker is conservative: it is written immediately before entering the browser handler, not at the exact portal submit click. If a worker expires after this marker without a stored response, the outcome is unknown and requires operator investigation; do not retry it automatically until paid-action idempotency is explicit.

**Acceptance criteria.** Every accepted request remains queryable after restart and reaches a documented state; failures include a stable error category; recovery does not duplicate a paid submission.

### 1.5 Resolved on 2026-09-25 (uncommitted)

The test-coverage work found these defects, and they were fixed in the working tree. All four were already present in `HEAD`; the dispatch bugs date from the `BrowserManager` extraction (956e4a6).

| Defect | Impact before the fix | Fix | Regression tests |
|---|---|---|---|
| `esegui_generic` always passed `foglio`/`particella`, and passed them twice when they were also in `params` | Every indirizzo, partita, nota, export_mappa, originali, fiduciali and elaborato_planimetrico request raised `TypeError`; mappa, ispezioni and ispezioni_cart failed whenever `foglio` was supplied; `visura_immobile` failed on `tipo_catasto` | `_dispatch_kwargs()` in [browser.py](../sister/browser.py) passes only the keywords each dispatcher accepts, maps English aliases (`sheet`, `parcel`, `subunit`, `section`, `urban_section`) to Italian names (Italian wins), and passes missing location keys as `None` so `run_visura_immobile`'s sample defaults (Trieste F.9 P.166) cannot leak into a query | `test_every_generic_dispatcher_can_be_called_with_route_params` (signature-checked fakes for every real dispatcher), `test_generic_visura_immobile_never_falls_back_to_sample_defaults`, `test_italian_param_name_wins_over_english_alias` |
| `esegui_ispezione_ipotecaria` passed `subalterno`/`sezione`, which `run_ispezione_ipotecaria()` does not accept, and never forwarded the fiscal code, identifier or note number/year held by the request | Every ispezione ipotecaria failed; once that was fixed, searches by soggetto or nota would still have run without their key input | Pass `codice_fiscale`, `identificativo`, `numero_nota`, `anno_nota`; drop the unsupported arguments | `test_ispezione_ipotecaria_maps_search_type`, `test_ispezione_ipotecaria_forwards_subject_and_note_identifiers` |
| After the auth wait timed out, the worker called `request_queue.task_done()` before `continue`, and the `finally` block called it again | The resulting `ValueError` escaped the `finally` and ended the worker task, so no further request was processed until restart. With several items queued, the queue counter went wrong silently instead | Remove the duplicate call ([services.py](../sister/services.py)) | `test_worker_survives_auth_timeout_and_keeps_serving` |
| `find_result_rows`, `count_total_result_rows` and `count_result_rows` used `with sqlite3.connect(...)`, which commits but does not close the connection | One leaked SQLite connection per web results-page query (`ResourceWarning: unclosed database`) | `with closing(sqlite3.connect(...))` ([database.py](../sister/database.py)); these queries are read-only | `test_result_queries_close_their_sqlite_connections` |

## 2. SISTER session and CIE authentication

These findings concern the sibling `aecs4u-auth` package and captured portal pages. Confirm them against the exact dependency revision deployed by SISTER before changing either repository.

**Reported symptom:** after a broken session, a new session may be blocked until the portal-side session expires. Captures in `logs/pages/` (six runs dated 2026-09-09) reportedly show CIE login marked complete while still on `idserver.servizicie.interno.gov.it`, followed by a SISTER login page. Treat the captures and behavior as evidence to re-check, not a guarantee about current portal behavior.

### 2.1 Validate authentication completion

| Reported issue | Location | Proposed correction |
|---|---|---|
| Checking whether `agenziaentrate.gov.it` occurs anywhere in the URL can match the CIE page's encoded `SPName` query parameter | [`cie.py`](../../aecs4u-auth/aecs4u_auth/browser/auth/cie.py), [`services/sister.py`](../../aecs4u-auth/aecs4u_auth/browser/services/sister.py) | Parse the URL and compare the hostname against the expected relying-party host(s) |
| A failed `navigator.navigate()` result is ignored | [`manager.py`](../../aecs4u-auth/aecs4u_auth/browser/manager.py) | Treat `False` as a failed transition and return a typed failure |
| Keep-alive moves the mouse but may not make a server request; `check_session` reportedly inspects the current DOM only | [`manager.py`](../../aecs4u-auth/aecs4u_auth/browser/manager.py) | Verify session state with a safe authenticated SISTER request on a measured interval |

**Implementation status (2026-09-26).** CIE completion and SISTER readiness validate URL hostnames; a `False` result from service navigation raises `BrowserNavigationError`. `check_session` makes a safe authenticated request to the SISTER Visure page, and the manager uses it for keep-alive. Browserless auth regressions cover the hostname check and server-contact behavior (56 focused auth tests pass). The request has not been checked against the live portal.

**Acceptance criteria.** Login is reported ready only after a known authenticated SISTER page/state is observed; navigation failure cannot be reported as success; the keep-alive check is verified to contact the portal and not trigger a paid action.

### 2.2 Reuse and recover the existing session

Proposed startup sequence:

1. With CDP, inspect existing tabs and reuse a SISTER tab only after an authenticated-state check succeeds.
2. If needed, try a known session-recovery page with existing cookies.
3. Start CIE authentication only when recovery lands on an actual login/expired-session state.
4. Do not log out during routine shutdown by default. Keep explicit logout available through the operator interface or a clearly named configuration option.
5. Without CDP, use a persistent browser profile if the auth library supports it safely.

**Implementation status (2026-09-26).** `ensure_service_session("sister")` now attempts authenticated tab reuse, cookie recovery, then CIE login, and records `reused`, `recovered`, or `authenticated`. Local browser shutdown logs out by default; CDP disconnect does not close the remote browser or log out by default. The local profile remains non-persistent, so reuse without CDP is still limited to the current browser process. Restart reuse and logout behavior need controlled portal verification.

### 2.3 Handle a locked session page

A captured `error_locked.jsp` page dated 2026-04-12 reportedly offered “Chiudi” links to `/Servizi/CloseSessionsSis` and `/Servizi/CloseSessions`. `_try_close_stale_session` ([services.py](../sister/services.py)) is reported to call close URLs without retrying the original navigation or closing the locked tab.

**Implementation status (2026-09-26).** The auth navigator recognizes the locked page, follows a matching portal-owned “Chiudi” link in the same context, and performs one bounded recovery attempt. It raises `SessionLockedError` if the page remains locked. Browserless tests cover the transitions (56 focused auth tests pass); the captured links and whether this clears a live lock still need verification.

### 2.4 Move SISTER session ownership into `aecs4u-auth`

CIE login itself already lives in `aecs4u-auth` ([`auth/cie.py`](../../aecs4u-auth/aecs4u_auth/browser/auth/cie.py)); nothing about the identity-provider flow needs to move. What is split is the **SISTER session lifecycle** around it. The fixes in §2.1–2.3 should land in one place, so this is the recommended home for them.

**Code finding.** Session behavior is currently split between the library and SISTER, and SISTER depends on library internals:

| Responsibility | In `aecs4u-auth` | Duplicated or patched in SISTER |
|---|---|---|
| Navigate ADE → SISTER; recover from `login.jsp` | `SisterNavigator.navigate()` / `recover_session()` | Public navigator entry point delegates start-page navigation |
| Detect “Utente gia' in sessione” | `SisterNavigator` raises `SessionLockedError` | Typed error is preserved through the SISTER worker/API |
| Close a stale portal session | `SisterNavigator` follows the portal's matching “Chiudi” link | No SISTER-owned close URL or fallback remains |
| Browser/context options | Public `BrowserConfig` options | SISTER sets `no_viewport` and launch args before manager initialization |
| CDP reconnect | Public `reconnect()` / `is_connected` API | SISTER wrapper delegates to the auth manager |

SISTER no longer accesses private `aecs4u-auth` attributes in its browser wrapper. In the local checkout, opendata's source does not import `aecs4u_auth.browser`; confirm deployed consumers before relying on this inventory.

**Proposal.** Make `aecs4u-auth` the single owner of the SISTER session. It would expose:

- one public entry point (for example `ensure_service_session("sister")`) that returns a verified SISTER page after trying, in order, tab reuse, cookie-based recovery, and full CIE authentication (§2.2);
- locked-session handling (“Chiudi”, then one bounded retry) and the close-session URLs (§2.3);
- a keep-alive that contacts the server, and an explicit logout policy (`logout_on_shutdown`, default off in CDP mode) (§2.1–2.2);
- typed errors (`SessionLocked`, `SessionExpired`, `MFATimeout`) and a reported session origin: `reused`, `recovered` or `authenticated`;
- browser options as public configuration (launch args, `no_viewport`, persistent profile) instead of monkeypatching.

SISTER keeps query automation: form filling, extraction, CAPTCHA/document handling (§3; the CAPTCHA appears on document requests, not on login), request lifecycle and workflows. It calls only the public entry point, which removes `_try_close_stale_session`, the retry block in `_navigate_to_scelta_servizio`, and all `_auth._…` access.

**Implementation status (2026-09-26).** The public session API, locked-session handling, server-contact check, browser options, and CDP reconnect/disconnect behavior are implemented in the sibling `aecs4u-auth` worktree. SISTER now uses that API, exposes the session origin in auth status and `/health`, and no longer owns the lock-close URLs or ADE fallback. Auth regressions pass (56 tests); SISTER dispatch/worker regressions pass with the sibling auth source on `PYTHONPATH` (72 tests). This is source-level completion only: no live portal session was run, no auth package release/version pin was made, and SISTER's current workspace dependency configuration cannot be resolved by the installed `uv` because the ancestor workspace contains a nested workspace. Verify cold login, restart reuse, lock recovery, and shutdown against the deployed dependency before release.

**Sequencing.**

1. Land the §2.1 fixes and the new public API on an `aecs4u-auth` branch, with unit tests for the URL/hostname checks and page-state detectors. These tests need no browser.
2. **Done in the working tree:** switch SISTER to the public API, remove duplicated session handling, and expose the session origin. Verify a controlled live session: cold start, service restart with reuse, and locked-session recovery.
3. Resolve the ancestor `uv` nested-workspace configuration or use a released `aecs4u-auth` package; then pin the minimum dependency version and verify the deployed dependency.

**Acceptance criteria.** Source criteria are met: SISTER has no private auth-manager access or SISTER-owned lock/session URLs, locked recovery is bounded, and the health endpoint reports session origin. Deployment criteria remain open: restart reuse without a new CIE approval and lock recovery must be verified against the live portal and released dependency.

## 3. CAPTCHA and document requests

### 3.1 Separate extraction from document submission

Which pages show a CAPTCHA, with evidence and limits: [captcha_pages.md](captcha_pages.md).

A saved-page audit reports 79 CAPTCHA pages among 622 runs (68 `visura_immobile`, 6 `visura_soggetto`, 4 `visura_inoltrata`, 1 `intestati`). These counts describe the captured sample only. The reported intestati flow can submit both a soggetto document request and an immobile document request for each subunit; confirm current behavior and charges with the portal before estimating volume.

**Proposal.** Add an explicit `request_documents` option, defaulting to `false`, to flows that can both extract on-page data and request official documents. Keep the search/extraction result available when document requests are disabled. Make the paid/official-document consequence visible in CLI and web UI before submission.

**Implementation status (corrected 2026-10-06).** Not present in the source tree: `request_documents` appears nowhere in `sister/`, `VisuraClient.intestati()` has no such parameter, and `run_visura` still submits the document requests (`visura_soggetto=True`, plus Visura per Immobile) whenever `extract_intestati` is on. The 2026-09-26 note that this was implemented, and `tests/test_client_contract.py::test_intestati_document_request_is_opt_in_and_forwarded`, describe work that is not in this working tree. No live portal run has been performed.

**Acceptance criteria.** Default extraction performs no document submission; opting in is explicit and auditable; tests verify that the flag is propagated and that the false path never reaches document-submit actions.

### 3.2 Record CAPTCHA timeouts as pending human work

[`_wait_for_captcha`](../sister/utils.py) currently logs that it is “proseguendo comunque” after a timeout. If the request is then stored as submitted, the recorded state does not match what happened.

**Proposal.** On timeout, preserve the request as `needs_human` (or a document-specific pending state), keep enough state to resume safely, and do not claim submission or success. Add an operator flow that presents the CAPTCHA image and accepts a human-entered code if continued browser automation is permitted by the portal's terms and agreement. Notify operators only after a durable pending item exists.

**Implementation status (2026-10-06).** `CaptchaRequired` (`sister/models.py`) is raised by `_wait_for_captcha` on timeout or after 5 wrong codes. `run_visura` keeps the extracted data and adds `needs_human` to its result; other flows propagate it. Polling (`GET /visura/{id}`, web form, client, CLI) reports status `needs_human` and stops. Still missing: durable `needs_human` job state (`_request_job_outcome` and the request-job rows that `tests/test_service_worker.py` and `tests/test_request_jobs.py` expect are not in the source) and the operator resume flow. A note on which pages carry a CAPTCHA is in [captcha_pages.md](captcha_pages.md).

**Policy.** Do not automate CAPTCHA solving. The CAPTCHA protects official document requests under the SISTER agreement; confirm permitted automation and higher-volume options with Agenzia delle Entrate.

## 4. Reliability and workflow operations

### 4.1 Persist request/job state before expanding queue features

**Implementation status (2026-09-26).** SISTER persists each accepted request before queueing, claims with a conditional database update, and renews a 180-second lease every 30 seconds while waiting for authentication or portal work. Startup and periodic recovery leave live leases untouched, requeue expired jobs only before the handler marker, and persist `interrupted_unknown` after the marker. A saved response takes precedence over recovery. Read-only or unavailable persistence fails closed, and queue clear stores a cancelled outcome. Retries and operator resume remain deferred until portal idempotency is defined.

A database-backed job table supports restart recovery, visibility, cancellation, and future retry, with `queued`, `running`, `done`, `no_match`, `failed`, `retry_wait`, and `needs_human`, plus attempts, error category, timestamps, a lease, and a request/result reference. The handler marker is conservative: it is set immediately before dispatch, and dispatch is blocked if that marker cannot be saved, so failures after it is saved are treated as possibly externally visible.

**Acceptance criteria.** Concurrent workers cannot claim the same job; an unexpired lease is not recovered; expired pre-handler jobs requeue; expired post-handler jobs become unknown and are never automatically replayed; terminal states and cancellation are visible through API/CLI. These source-level paths now have regression coverage. Live portal side-effect timing and deployed restart behavior remain unverified.

### 4.2 Typed errors, retry policy, and `no_match`

Service responses now expose stable `error_kind` values for session locks/expiry, portal unavailability, invalid input, selector drift, CAPTCHA-required work, and unclassified portal errors. The CLI renders `no_match`, `needs_human`, `cancelled`, and failure outcomes separately. No-match is stored as a distinct status with the portal message preserved as detail.

Retries remain deferred: only transient, idempotent operations may be retried with bounded backoff, and portal submission idempotency must be defined first.

**Acceptance criteria.** API and CLI expose stable outcome categories; permanent input errors are not retried; temporary failures use bounded retries; no-match is distinguishable from both success and failure.

**Related code finding: the ontology drops fields.** The Pydantic models in [ontology.py](../sister/ontology.py) ignore extra keys, so they silently drop fields that real payloads carry:

- `DatiSoggetto` and `DatiRicercaImmobile` have no `error` field, so the "NESSUNA CORRISPONDENZA TROVATA" signal is lost. `DatiPersonaGiuridica` does keep it.
- `DatiElencoImmobili` has no `provincia`/`comune`/`foglio` for the area that was listed.

The schemas now preserve these fields. `tests/test_ontology.py::test_ontology_does_not_drop_payload_fields` covers the payload contracts as normal passing tests.

### 4.3 Workflows: use persisted runs already owned by the workflow service

Named and custom CLI workflows now submit ordered step definitions to opendata's persisted workflow engine; SISTER no longer orchestrates custom steps in the CLI process ([cli.py](../sister/cli.py), [workflow engine](../../opendata/opendata/workflows/engine.py)). The shared input model validates custom steps and the engine applies the existing depth, fan-out, paid-step, and total-step limits. SISTER proxies run status, cancellation, and resume requests to opendata.

The owner service persists the run and each step checkpoint; SISTER-specific browser execution remains behind its existing API boundary. Deployment-level workflow execution and restart-resume still need verification against the deployed opendata API.

**Implementation status (2026-09-26).** SISTER preset/custom CLI submissions and run status/cancel/resume commands share the persisted opendata run lifecycle. Contract tests cover SISTER's proxy boundary; opendata tests cover custom step order, required inputs, paid/depth guards, and interrupted paid-step review. Live deployed integration and resume across an actual interrupted run remain unverified.

**Acceptance criteria.** CLI, web UI, and scripts submit compatible workflow definitions and use the same run lifecycle; interrupted runs can resume from checkpoints without repeating completed paid steps. Source-level paths are implemented; deployment-level acceptance remains open.

### 4.4 Batch jobs: extend existing CLI support

The project already has `sister query batch`; a generic batch command/API is therefore not a net-new proposal.

**Implementation status (2026-09-26).** CLI CSV batches validate the complete checkpoint before submitting rows, write updates through an atomic temporary-file replacement, bind resume to the CSV hash and default command, reuse saved request IDs, and skip completed rows. The CLI writes a `submitting` checkpoint before each POST; after a crash or transport error, it marks the row `submission_unknown` and never resubmits it automatically. Resume re-polls known request IDs; timeouts preserve those IDs for safe polling. Known validation errors are stored as terminal row errors. There is no automatic retry policy for completed SISTER requests, because portal idempotency is not defined. Add a server-side `POST /batch` only if callers need durable server-managed batches or progress across client disconnects.

Keep the initial scope small: retry only rows with retryable outcomes and never resubmit a row whose portal outcome is unknown.

**Acceptance criteria.** Resume does not resubmit completed rows; partial failures are visible; checkpoint corruption or mismatched input fails clearly; server-side batching has an explicit lifecycle and idempotency contract before it is added.

### 4.5 Smaller operational improvements

- **Implemented in the working tree:** paid ispezione cache hits are disabled unless the API/CLI caller explicitly sets `reuse_cached` / `--reuse-cached`; `--force` bypasses cache. The CLI says when it reused a cached paid result.
- Configure cache freshness by request type; a single global freshness TTL is still in use.
- Add completion callbacks only with authentication, retry limits, and a clear delivery guarantee; polling remains available.
- Track failure rates by page/step type and alert on sustained selector drift. Use `page_visits` and structured error categories rather than raw log text alone.
- **Resolved in the working tree:** `VisuraIntestatiInput` and `VisuraInput` validate `tipo_catasto` and reject values outside the endpoint’s supported set, so malformed values no longer fall through to a Terreni query.

## 5. Typed page-state handling and possible AI use

Represent browser automation as a state machine: each recognized page has a detector based on URL and DOM markers, an allowed action, and an expected next state. Unknown states should stop the flow with `SelectorDrift` and retain the page capture for diagnosis instead of continuing with an assumed page.

**Implementation status (2026-09-26).** Deterministic detectors and `SelectorDrift` checks now guard the primary immobile visura transitions (office selection, service menu, search form, and search result), and unknown document-request pages stop with a capture. Offline tests cover the detector outcomes. Other portal flows still need explicit state contracts; this is a partial implementation.

The referenced [TypeSafe AI Jev announcement](https://typesafe.ai/blog/introducing-system-one-models-and-jev) describes a hosted model and vendor-reported confidence/latency claims. Verify that the service, terms, and model are currently available before treating those claims as implementation options.

| Candidate use | Recommendation |
|---|---|
| Ambiguous text matching (comune/sezione) or ranking candidate result rows | Evaluate as a fallback only; require a confidence threshold and a deterministic/human fallback |
| Classifying an unfamiliar page from sanitized DOM or screenshot features | Consider for diagnostics after deterministic state detection fails; never let classification authorize a paid action |
| Navigation, form filling, session recovery | Keep deterministic and state-checked |
| CAPTCHA handling | Do not use a model to solve or bypass CAPTCHA |

Before any hosted-model experiment:

- Establish a lawful basis, data-processing terms, retention controls, and transfer safeguards for personal data such as tax codes and ownership information.
- Minimize and redact evaluation inputs; do not send production personal data by default.
- Build an interface that permits a rule-based fallback and can be disabled without changing extraction behavior.
- Evaluate on a held-out, labeled set of historical cases; measure accuracy, abstention, and error cost by decision type.
- Require human review for low-confidence or high-impact decisions, and never let the model initiate paid portal actions.

## 6. Implementation gates

1. Confirm source-level findings against the current branch; P0 SISTER defects have normal regression tests. Verify the workflow route and auth dependency against deployed versions before release.
2. Response retention is explicit in days and disabled by default. The request handler boundary is persisted conservatively; before adding retries or paid-action resume, define the exact portal submission boundary and idempotency rules.
3. Implement the low-risk route/signature fixes and outcome reporting, then verify them at their public API boundaries.
4. Source-level CIE/session changes are implemented in `aecs4u-auth` behind its public API (§2.4). Before release, verify CIE completion, restart reuse, and locked-session recovery with captured states and a controlled live session.
5. Opt-in document submission and CAPTCHA `needs_human` outcomes are implemented in source. Verify their portal behavior and permitted operator handling before building a CAPTCHA resume panel or notification path.
6. Revisit AI only after the state machine, evaluation data, and privacy review are ready.

## 7. Test coverage and regression tests

A historical coverage run on 2026-09-25 raised measured coverage from 35% to 44% of `sister/` (512 passing tests at that time). On 2026-09-26, the full SISTER suite completed with 584 passed and 1 skipped. The opendata custom-workflow owner tests completed with 6 passed. No test drives the live SISTER portal: browser functions are replaced by fakes that check real call signatures, so portal and deployed-service behavior still needs controlled verification.

| Module | Before | After | New test file |
|---|---|---|---|
| `extraction_jobs.py` | 0% | 100% | `tests/test_extraction_jobs.py` (job storage, service lifecycle, `/sezioni/extract` routes) |
| `ontology.py` | 0% | 100% | `tests/test_ontology.py` (fixture payloads against the schemas) |
| `services.py` | 51% | 84% | `tests/test_service_worker.py` (worker dispatch, cache, batches, auth and browser control) |
| `database.py` | 55% | 85% | `tests/test_database_queries.py` (cache lookup, JSON→table projection, results queries) |
| `client.py` | 54% | 83% | `tests/test_client_contract.py` (client against the real app in the same process) |
| `routes.py` | 49% | 62% | covered by the files above |
| `browser.py` | 25% | 58% | `tests/test_browser_dispatch.py` (`esegui_*` argument mapping and error wrapping) |

`utils.py` (3%) and `web.py` (21%) remain the largest untested modules; `utils.py` is mostly portal automation and is intentionally out of scope.

**Known-defect tests.** The four SISTER-side defects listed in the 2026-09-25 snapshot were fixed in the working tree, and their xfail markers have been removed. The ontology field-loss cases are also fixed. The workflow contract test reaches the real SISTER route but stubs the opendata transport, so deployed integration remains unverified.

**Current verification.** On 2026-09-26, `PYTHONPATH=.:../aecs4u-auth:../aecs4u-workflow pytest -q` completed with 584 passed and 1 skipped. The run reported one `LogfireNotConfiguredWarning` and one existing aiosqlite event-loop shutdown warning. `PYTHONPATH=.:../opendata:../aecs4u-auth:../aecs4u-workflow pytest -o addopts= -q ../opendata/tests/test_workflow_custom_steps.py` completed with 6 passed. Neither suite exercises the live SISTER portal or deployed cross-service integration.
