# CLI architecture plan

Status: code phases 0–7 implemented, reviewed 2026-11-18. Local, mocked, and read-only checks are recorded below;
authenticated live verification remains pending. Scope: the `sister` command line (`sister/cli/`,
`sister/command_tools/`, and the shell helpers it forwards to). The original `sister/cli.py` has moved into the
`sister/cli/` package. `_commands.py` assembles the root app and defines root commands; group modules own most other
commands. The service, client contract, and `query_forms.py` change only where a phase says so.

### Implementation snapshot (2026-11-18)

| Phase | Current state in this checkout |
|---|---|
| 0 — Safety fix | Implemented: paid workflow confirmation uses `--confirm-paid`; deprecated workflow `--yes/-y` fails, and the deep/full paid guard is checked before the request. The hidden `--yes/-y` options on the legacy ipotecaria commands remain ignored for compatibility. |
| 1 — Package split | Implemented: command groups live in focused modules; `_commands.py` assembles the root and owns root-only commands. `query.py` contains generated query forms, batch execution, and a hidden deprecated workflow alias. `__init__.py` lazily exposes only `app` and `run`; the forwarding registry and `tools.py` are removed. The admin launcher reads its allowlisted commands from the Typer tree. Cold root help measured 0.38 s median across three runs with stdout redirected. |
| 2 — Shared runtime | Implemented: all API requests use `run_api`; `run_db` handles local async database operations; the two `VisuraAPIError` catches are in `_runtime.py` and the multi-request query path. `emit` owns data-result `--output` writes. Hidden legacy artifact flags on `batch convert-list` and `documents define-workflow` retain their old destinations with warnings; their canonical names are `--destination` and `--manifest`. The remaining direct `asyncio.run` calls are local login and floor-plan work. Pure renderers and exit handling are covered by focused tests. |
| 3 — Machine-readable output | Implemented: root `--json` and `SISTER_CLI_JSON=1` route Rich progress and hints to stderr. Data commands emit one JSON document. Local tools whose report was text use a shared `{command, exit_code, output}` envelope, including module-level Rich consoles; commands with structured reports emit those reports directly. Coverage includes health, `queries`, requests/history, every generated query dry-run, batch execution/schema, workflow run/status/cancel, mortgage status/quote, database migration/status/backfill, AdE imports/linking, floor-plan validation, receipt imports (including an empty directory), P7M/workflow reports, password hashes, and local tool reports. |
| 4 — One definition per query | Implemented: paid immobile/company/note inspections are `QueryForm` entries with fixed `tipo_ricerca`, `auto_confirm=false`, paid help/list markers, and hidden ignored `--yes` compatibility. The free persona-fisica preview is the shared target of hidden `query ipotecaria-persona` and `mortgage start` aliases. Paid forms are excluded from batch schemas/plans and MCP submission; `list_queries` marks them. `queries` builds form rows from `QUERY_FORMS` and other API rows from registered Typer callback metadata. |
| 5 — One workflow engine | Implemented in the SISTER and OpenData checkouts: OpenData accepts `preset=custom` and ordered `custom_steps`; SISTER prepares the four start shapes, exposes `workflow run` with repeatable `--with`, and keeps `query workflow` as a hidden deprecated alias. `test_workflows.py` pins the expected legacy start sequences, including parcel searches with and without a subalterno, and checks ordered optional steps. The old client-side flow is no longer runnable, so a direct old-versus-new execution comparison cannot now be made. The OpenData run endpoint persists and returns a queued workflow ID before background execution; wait mode polls status. Live service verification remains pending. |
| 6 — Command tree cleanup | Implemented: `requests get|wait|list`, migration revision/status comparison, `documents floor-plan-validate`, root help panels, Italian CLI messages, and hidden compatibility aliases are implemented and covered. The active CLI service at `http://localhost:8025` returned matching rows for pending/completed/failed status queries, so the client-side fallback has been removed. |
| 7 — Port forwarded tools | Implemented: batch session/progress/list/PDF-parseable/Recrowd workflows, all app commands, all document tools, both build commands, and all UI audits have Typer entry points. `app start` delegates Uvicorn startup to `scripts/start.sh`; retained batch shell scripts are compatibility launchers to the CLI, and `dev fixture-payloads` is the only developer shell command. Standalone `python -m sister.command_tools...` entry points remain where they existed. Input-list and document maintenance commands preview by default and write only with `--apply`. |

Verification: the last aggregate focused SISTER CLI/query-form/batch/workflow/render/runtime/JSON/MCP run passed
533 tests. It reported the existing pytest-asyncio configuration notice and a Logfire-not-configured warning; two
runtime tests added afterward were not part of that aggregate run. Ruff passed for `sister/cli/`, the focused test
modules, and the added batch, PDF/PVP, and audit runner modules in the preceding review. The wheel build succeeded,
included `sister.cli`, and `python -m sister.cli --help` worked from the built wheel. A read-only
`sister --json health` probe against the local service returned valid JSON on stdout and a `degraded` status with
authentication unavailable because the browser was not running. OpenData's focused workflow route/custom-step
tests passed 11 tests with coverage disabled for that focused run, and Ruff passed for the changed workflow files.
The active CLI service at
`http://localhost:8025` returned five matching rows for each of `pending`, `completed`, and `failed` using both the
API endpoint and `sister --json requests list --status STATUS --limit 5`; the CLI no longer re-filters those results.
Authenticated-session checks have not run: query, workflow, `batch run-list`, `batch run-session`, and the
resulting `query batch` execution remain pending. The health probe reported no authenticated browser session, so
only local, mocked, and read-only paths are confirmed here. The focused test result above predates two later runtime
test additions; it must not be treated as their aggregate test result. This is not a full-suite or release claim.
Update the snapshot when a phase changes state.

## 1. Why

At plan creation, `sister/cli.py` was 2,276 lines and had grown one command at a time. It works and is reasonably tested
(`tests/test_cli.py`, `tests/test_cli_commands.py`); after the package move, query and batch logic remains concentrated
in `sister/cli/query.py`, while `_commands.py` mounts groups and defines the remaining root commands. The problems
below describe the original baseline unless marked as current:

- **Four kinds of command share one module with nothing telling them apart.**

  | Kind | Commands | Needs |
  |---|---|---|
  | API client (HTTP) | `query *`, `get`, `wait`, `requests`, `history`, `health`, `queries`, `workflow *`, `mortgage *` | running service, `VISURA_API_URL`/`VISURA_API_KEY` |
  | Direct database | `db *`, `ade *`, `documents import-receipts` | selected `SISTER_DATABASE_DSN_*`, no service |
  | Local files | `floor-plan-validate` | nothing |
  | Forwarded tools | `app *`, `batch *` (except `query batch`), `documents *`, `build *`, `audit *`, `dev *` (22 Python modules, 6 shell scripts) | a checkout (`_project_root`) |

- **Two ways of building commands.** Typer commands run in the main process. The forwarded tools are argparse programs
  started as `python -m sister.command_tools.<x>` or `bash scripts/<x>.sh`. Help, option names (`--dry-run` vs
  `--apply`, the meaning of `-o`), exit codes and output helpers differ, and every call starts a new interpreter.
  `app stop` runs `../bin/kill_app.sh`, a file outside the repository.
- **Some queries are hand-written even though the project rule says to define them in `query_forms.py`.**
  - The ispezione ipotecaria for persona fisica has **three** entry points: `query ipotecaria-persona` (hand-written),
    `query ipotecaria-persona-fisica` (generated from `QUERY_FORMS`) and `mortgage start`.
  - `query ipotecaria-immobile|-azienda|-nota` are three more hand-written copies of the same body (now in
    `sister/cli/query.py`).
- **Two workflow engines.** `query workflow --preset` runs on the server (`workflows.py`, `POST /catasto/workflow/run`).
  Without `--preset`, the same command runs about 300 lines of workflow logic in the CLI itself (now in
  `sister/cli/query.py`).
  The API, the web UI and MCP can't use that second engine.
- **The same code in every command.** Most API-client commands repeat `try: asyncio.run(...) except VisuraAPIError:
  _handle_api_error(e); return`, plus the `--output` handling and the "error/expired/needs_human → exit 1" check.
- **Inconsistencies, one of them a safety trap:**
  - `--yes` means three things:
    - in the generated query commands, "download again" (may be charged);
    - in `query ipotecaria-*`, a leftover option that explicitly does **not** authorize a payment;
    - in `query workflow`, "automatically confirm paid costs" (`auto_confirm=yes`).
  - `db init` and `db migrate` run the same code (`alembic upgrade head`).
  - `requests` and `history` call the same endpoint (`/visura/history`).
  - At plan creation, the no-preset `query workflow --dry-run` printed `POST /api/v1/visura/workflow`; the real path is
    `/api/v1/catasto/workflow/run`. The `queries` table is written by hand next to the generated rows, so it can go
    stale the same way.
  - Help text is in Italian, run-time messages in English, and the forwarded tools mix both.
  - Group variables are named inconsistently (`query_app`/`db_app` vs `app_tool`/`batch_tool`). There is also a group
    named `app`.
- **Slow start (original baseline).** `sister --help` took about 1.0 s, and about 0.5 s of that was `import
  sister.batch`, which pulled in logfire. Every command paid it, `sister health` included. The batch import is now
  on demand; cold root help measured 0.38 s median across three runs in this checkout.

## 2. Goals and non-goals

Goals:

1. One way to write a command: Typer, in the main process, with shared options, error handling and output.
2. Every single-step query, paid preview queries included, comes from `query_forms.py`.
3. One workflow engine (the server's).
4. Each command group sits in its own module, only imports what it uses, and says what it needs (service,
   database, checkout).
5. Option names mean the same thing everywhere; paid actions are never one flag away.
6. Machine-readable output (`--json`) for every command that prints data.
7. No behaviour change for scripts and batch runs except where this plan says so; deprecated names keep working
   for at least one release with a warning.

Non-goals:

- Changing the service API, the batch file format (`docs/batch_format.md`) or the journal format.
- Browser automation tests (deprioritized, see `CLAUDE.md`).
- Rewriting the forwarded tools' logic: they are ported as they are, only their entry points change.

## 3. Target layout

```
sister/cli/
  __init__.py      lazy facade for app/run()
  _commands.py     constructs the root Typer app, mounts groups, defines root-only commands
  _runtime.py      console, run_api(), run_db(), emit(), exit_for_status(), project_root(), shared options
  render.py        result, request, history, workflow and health renderers: dict -> Rich renderable (no I/O)
  query.py         generated single-step commands (QUERY_FORMS), `batch`, `batch-schema`
  workflow.py      `workflow run|status|cancel`
  requests.py      `requests get|wait|list`
  db.py            `db migrate|status|backfill`
  ade.py           `ade statement import|link-pagopa|detail-import`, `ade receipt import`
  mortgage.py      `mortgage start|status|quote`
  documents.py     Typer wrappers of the document tools
  batch_tools.py   Typer commands for batch preparation and session workflows
                   (`batch run-list|run-session|progress|build-list|...`)
  app_tools.py     `app login|start|stop|hash-password`
  build_tools.py, audit_tools.py
  dev.py           developer-only fixture command (shell wrapper)
```

`sister.cli` stays the import path: the entry point in `pyproject.toml` (`sister = "sister.cli:run"`) does not change.

### 3.1 `_runtime.py`

```python
console = Console()

def run_api(coro_factory: Callable[[VisuraClient], Awaitable[T]], *, client: VisuraClient | None = None) -> T:
    """asyncio.run() inside `async with client`; VisuraAPIError -> styled message + exit 1;
    httpx.ConnectError -> 'service not reachable at <url> (sister app start?)' + exit 3."""

def run_db(coro_factory: Callable[[], Awaitable[T]]) -> T:
    """asyncio.run(database.init_db(); coro_factory()); a missing DSN -> clear message + exit 3."""

def emit(data, *, output: Path | None, fmt: OutputFormat, render: Callable[[Any], RenderableType] | None) -> None:
    """--json: json on stdout (nothing else on stdout); otherwise the Rich renderer; --output: also write the file."""

def exit_for_status(*results: dict) -> None:
    """exit 1 if any result is error/expired/needs_human."""

# shared Annotated options, one definition each
OutputOpt  = Annotated[Optional[str], typer.Option("--output", "-o", help=...)]
WaitOpt    = Annotated[bool, typer.Option("--wait", "-w", help=...)]
DryRunOpt  = Annotated[bool, typer.Option("--dry-run", help=...)]
ForceOpt   = Annotated[bool, typer.Option("--force", help=...)]
RedownloadOpt = Annotated[Optional[bool], typer.Option("--yes/--no", help=...)]
```

`run_api` replaces the current one-request-per-`asyncio.run` pattern: a command does all of its awaiting
in a single event loop with one HTTP connection pool (`VisuraClient` already supports `async with`). This removes the
workaround described in `VisuraClient._get_client`.

### 3.2 Exit codes (documented in `--help` of the root command)

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | the request ran and failed (error, expired, needs_human, failed rows) |
| 2 | invalid input (Typer's own usage errors, `validate_params`, invalid batch rows) |
| 3 | environment: service unreachable, database not configured, checkout not found |
| 75 | SISTER session expired (`EXIT_SESSION_EXPIRED`, unchanged) |
| 130 | interrupted |

At the original baseline, "service unreachable" exited 1 from `health` and raised a traceback from most other
commands. `run_api()` now maps `httpx.ConnectError` to exit 3; other environment and failure paths still need review.

### 3.3 Command tree after the plan

```
sister
  health | queries
  query     <generated QUERY_FORMS commands> | batch | batch-schema
  requests  get | wait | list                       (was: get, wait, requests, history)
  workflow  run | status | cancel                   (was: query workflow, workflow status|cancel)
  mortgage  start | status | quote
  db        migrate | status | backfill             (init kept as an alias)
  ade       statement {import,link-pagopa,detail-import} | receipt import
  documents import | import-receipts | import-ocular | smart-ocr | download-missing-p7m | rename-date
            normalize | cleanup-xml | reconcile | define-workflow | floor-plan-validate
  batch     run-session | run-list | progress | build-list | prune-list | convert-list | convert-pvp-csv
            build-pdf-parseable | run-pdf-parseable | run-pvp-is | recrowd
  build     dossier-graph | building-kmz
  app       login | start | stop | hash-password
  audit     sweep | interactions | demo-purchases | purchase-paths
  dev       fixture-payloads
```

Old names (`get`, `wait`, `history`, top-level `requests` with filters, `query workflow`,
`query ipotecaria-persona|-immobile|-azienda|-nota`, `floor-plan-validate`) stay as deprecated aliases that print one
line on stderr and call the new command. Root `requests` filters are handled by the canonical `requests` group's
no-subcommand callback because the old syntax and new group share the same name. The other aliases are hidden.
They are removed after one tagged release.

### 3.4 Batch entry points: separate workflows, shared execution

Keep `query batch` and `batch run-list` as separate commands. They overlap in reaching batch submission, but begin at
different points in the user's workflow and have different option ownership. The workflow has three roles: `run-list`
builds a plan and starts a session run, `run-session` manages that authenticated run and starts the row runner, and
`query batch` executes rows from the prepared plan.

| Layer | Owns |
|---|---|
| `batch run-list` (`sister/cli/batch_tools.py`; `scripts/run_list_in_session.sh` is a compatibility wrapper) | The list → plan coordinator for a supported Markdown list: records the login time before batch generation, picks the batch folder (resume or new), previews in a temporary folder for `--dry-run`, and calls `build-list` with list choices (`--extra`, `--captcha-free`, `--tipo-richiesta`). It then hands the plan and session options to `run-session`. |
| `batch run-session` (`sister/cli/batch_tools.py` → `sister/command_tools/run_in_session.py`) | The authenticated-session lifecycle: browser check, service start/stop and stale-service handling (`--stop-stale-service`), dropping answered rows, the preflight search, deadline and grace period. It launches `query batch` as a subprocess, then downloads ready documents from Richieste (`download_ready_documents`) and sends the progress email. |
| `query batch` | Row execution for a prepared JSON, Markdown, or CSV batch through the shared batch engine (`BatchRunner`): validation, batch options, retries, journal, `--resume`, `--stop-at`. It assumes the service is already running and does not own the browser session or a session schedule. |

Keep session-only options on `batch run-session`, list-only options on `batch run-list`, and generic submission and
journal options on `query batch`. No layer grows a second submission/retry/journal implementation: retries, resume
semantics, request submission and journal behaviour live only in the batch engine. `batch build-list` and
`batch run-session` remain reusable operations for `run-list` and other callers. `query batch` may keep accepting
Markdown directly; that convenience does not make it responsible for the authenticated-session workflow.

**Decision on merging:** if these are separate tracked tasks, group them under one batch-workflow parent. Keep the
CLI commands separate: `batch run-list` turns a source list into a plan and coordinates the session run;
`query batch` executes rows from a prepared plan. Their shared work belongs in `BatchRunner` and the reusable
`batch build-list` / `batch run-session` operations, not in one command with mixed list, session, and row execution
flags.

The command path is `sister batch run-list`, not `sister query run-list`. For example:

```sh
uv run --no-project sister batch run-list inputs/pvp_current.phase1.md --extra all --stop-stale-service
```

`--extra all` is a list-building choice. `--stop-stale-service` is accepted by `run-list` and forwarded to
`run-session`, where stale-service handling belongs. `query run-list` is intentionally not registered and Typer
reports “No such command 'run-list'.”

**`run-session` runs `query batch` as a subprocess, and keeps doing so.** `run_batch()` starts
`python -m sister.cli query batch -I … --resume --output-dir … -o … --stop-at …`; `--output-dir` stores the
resume journal, while `-o` writes the final report. After the grace period, it interrupts the subprocess with
`os.killpg(…, SIGINT)`. The subprocess gives a clean interrupt (the journal keeps
every state), separate logs, and a crash that does not take the session runner down. It follows that **part of the CLI
is an internal API**, kept stable by every phase and pinned by the help snapshot (§6):

- `python -m sister.cli` as the launcher (needs `sister/cli/__main__.py` once `cli` is a package, see Phase 1);
- `query batch -I --resume --output-dir -o --stop-at`;
- the preflight `query soggetto --cf --wait --yes -o` (`preflight()` in `run_in_session.py`; `--yes` = download again, the meaning
  Phase 0 keeps);
- exit code 75 for an expired session;
- the command line text `sister.cli query batch`, which `stop_stale_service()` matches to detect a batch still
  running before it stops a stale service.

## 4. Phases

Each phase is one PR, leaves the test baseline no worse than it was (see §6) and is releasable on its own.
Phases 1-3 are mechanical; 4-6 change behaviour; 7 is the long tail.

### Phase 0 — Safety fix (implemented)

The payment-confirmation trap was closed before the refactor.

- `query workflow`: rename `--yes/-y` (auto-confirm paid costs) to `--confirm-paid`. Keep `--yes/-y` as a hidden alias
  that **fails** with exit 2 and a message pointing to `--confirm-paid`. A silent alias would keep the trap.
- `query workflow`: refuse `--depth deep|full --include-paid` without `--confirm-paid` in the CLI, before the request
  is sent (the server already gates it; this makes the message come from the CLI).
- `query ipotecaria-*`: drop the leftover `--yes/-y` (it already does nothing but print a notice); keep it hidden
  and ignored for one release.
- Fix the preset `query workflow --dry-run` path to print `/api/v1/catasto/workflow/run`, built from
  `API_V1_PREFIX` like the generated commands.
- Tests: `test_cli_commands.py` — `--yes` on `query workflow` exits 2; `--confirm-paid` reaches
  `VisuraClient.workflow(auto_confirm=True)`.
- **Implemented:** README uses `--confirm-paid` only for paid workflow costs, and CLI examples use the canonical
  `workflow run` command. The `query workflow` alias remains hidden and deprecated for one release.

Done when: no command takes a flag named `--yes` that authorizes a payment.

### Phase 1 — Package split, no behaviour change

The package shell and launcher are already in place; do not repeat the file move. Finish the extraction from
`_commands.py` into the group modules in §3, keeping the CLI tree and behavior stable during this phase.

1. **Implemented:** `__init__.py` lazily exposes only `app` and `run`. Tests import `VisuraClient` from `sister.client`,
   runtime helpers from `sister.cli._runtime`, and patch the command module that owns each subprocess call. The admin
   command catalog walks the current Typer tree instead of depending on the removed forwarding registry.
2. Keep `_commands.py` as the root app assembly module only; command implementations belong in their group modules
   (root-only commands such as `health`, `queries` and `floor-plan-validate` may stay there).
3. Lazy imports: `batch`, `database`, `xml_ingest`, `workflows`, `floor_plan`, `ade_ledger` and alembic are imported
   inside the commands that use them. The generated query commands need only `query_forms` and `client` at import.
4. Rename the group variables to one scheme (`query_app`, `db_app`, `batch_app`, `documents_app`, ...). The `app`
   group becomes `app_tools_app` in code; its CLI name stays `app`.
5. Keep `sister/cli/__main__.py` (`from . import run; run()`) working; it is required by `python -m sister.cli`,
   `batch run-list`, and `batch run-session` (§3.4).
6. Keep `EXIT_SESSION_EXPIRED` in `sister/cli/_runtime.py` and use that shared value from
   `sister/command_tools/run_in_session.py`.

Done: command groups and compatibility exports are cleaned up; the forwarding module is removed; focused CLI/batch tests
pass; `python -m sister.cli --help` works; and cold root help measured 0.38 s median across three runs with stdout
redirected. The current CLI snapshot includes deliberate tree changes from later phases. Compare the scoped CLI diff,
not the whole worktree, which may contain unrelated changes.

### Phase 2 — Shared runtime and renderers

1. Implement `_runtime.py` (§3.1) and convert every API-client command to `run_api`.
   - The multi-request paths (`_report_submission`, `_wait_for_requests`, `batch`) await inside one loop.
     (`_ipotecaria_command` was deleted in Phase 4, not converted.)
2. Move `_print_result`, the request/history tables, the workflow step summary and the health table to `render.py`
   as pure functions returning Rich renderables, so they can be tested without a console.
3. Introduce the exit codes of §3.2. The only visible change is that "service unreachable" exits 3 with one line
   instead of a traceback.
4. Use the shared `Annotated` options everywhere; the generated commands (`_make_query_command`) take the same
   objects through the `common` table.

Done when: `grep -c "except VisuraAPIError" sister/cli/*.py` is at most 2 (`_runtime.py`, and `batch` if its runner
needs its own handling), and every `--output` goes through `emit`.

### Phase 3 — Machine-readable output

1. Root option `sister --json` (stored in the Typer context; `SISTER_CLI_JSON=1` sets it). With it:
   - stdout carries exactly one JSON document per command;
   - progress, hints ("Poll results with ...") and warnings go to stderr;
   - Rich colours are off.
2. `--output` keeps its meaning (also write the JSON to a file) and works with or without `--json`.
3. `batch` with `--json` prints the final report (`report.to_dict()`); per-row events go to stderr as JSON lines.
4. `queries --json` prints the generated command list (Phase 4 makes it fully generated).

Done when: `sister --json <command> ... | python -m json.tool` succeeds for every command that prints data. A
parametrized test covers it with the client faked.

### Phase 4 — One definition per query

1. **Ispezioni ipotecarie into `query_forms.py`.**
   - Add `paid: bool = False` to `QueryForm`. Add specs for `ipotecaria-immobile`, `ipotecaria-azienda` and
     `ipotecaria-nota` with `method="ispezione_ipotecaria"` and the `tipo_ricerca` as a fixed parameter (new field
     `fixed_params: dict[str, str]`, sent but not exposed as an option).
   - `ipotecaria-persona-fisica` stays (`mortgage_search_start`). The hand-written `query ipotecaria-persona` becomes
     a hidden alias of it.
   - `paid=True` forms:
     - their `--dry-run` says "SERVIZIO A PAGAMENTO";
     - their help starts with it;
     - `queries` and MCP `list_queries` mark them (`requests_documents` already does the equivalent for documents);
     - `auto_confirm` is always `False` from the CLI.
   - Delete `_ipotecaria_command`, `_ipotecaria_dry_run` and the four hand-written commands from `sister/cli/query.py`.
   - Normalize `documents="none"` on `ipotecaria-persona-fisica` to `""` (the documented "reads only" value) or
     document `"none"` in `QueryForm`.
2. **`mortgage start`** becomes an alias of `query ipotecaria-persona-fisica`. `mortgage` keeps `status` and `quote`,
   which are flow operations, not queries.
3. **`queries`** is generated:
   - one row per `QUERY_FORMS` entry (as today);
   - one row per registered non-query command, from its Typer command and an `endpoint` attribute set where the
     command is defined.
   - Drop the hand-written rows.
4. Regenerate `docs/batch_submission.schema.json` (`sister query batch-schema -o ...`). Keep `paid=True` forms out
   of batch schemas and plans; there is no `allow_paid` opt-in. A batch cannot run ipotecaria inspections.
5. MCP: `mcp_server.list_queries` exposes `paid`, and `submit_query` refuses `paid=True` forms (the preview/confirm
   flow for paid steps is the admin web page).

Done when: `grep -n "def ipotecaria_" sister/cli/` is empty, `tests/test_query_forms.py` covers the new specs, and
`tests/test_batch.py` passes with the regenerated schema.

### Phase 5 — One workflow engine

1. Server: use the existing OpenData `custom` preset and `custom_steps` contract to reproduce the client-side flow:
   - start step chosen from the inputs (soggetto, persona giuridica, indirizzo, or search → intestati per subalterno);
   - optional steps from a `with` list: `elenco`, `mappa`, `fiduciali`, `originali`, `nota`, `ispezioni`,
     `ispezioni_cart`.

   The step logic already exists as presets' steps; this preset only selects them.
2. CLI: `workflow run` replaces `query workflow`.
   - `--preset` defaults to `custom`.
   - The seven `--elenco/--mappa/...` flags become one repeatable `--with elenco --with mappa` (the old flags stay
     as hidden aliases for one release).
   - Delete the client-side orchestration in `sister/cli/query.py` and `_run_step`.
3. `workflow run` prints the workflow id at once. The OpenData `/run` endpoint persists the queued run and starts
   execution after returning its ID; `--wait` (default on, as today) follows `GET /catasto/workflow/runs/{id}` until
   a terminal status. `--no-wait` returns after acceptance. A long workflow can then be picked up with
   `workflow status <id>`.
4. Tests:
   - `tests/test_workflows.py` gets the `custom` preset (plans for each start shape).
   - The `tests/test_cli.py` case for client-side search → intestati is rewritten against a faked
     `VisuraClient.workflow`.
5. Regenerate `docs/property_visura_workflow.svg` if the step list shown there changes.

Done when: the CLI holds no workflow logic, and `workflow run --dry-run` prints the server's plan
(`prepare_workflow`) for every preset, `custom` included.

### Phase 6 — Command tree cleanup

1. `requests get|wait|list`.
   - `list` merges `requests` and `history`: it has the filters of both, plus `--status`, and the columns of
     `requests` (a superset).
   - `get`, `wait`, `history` and the top-level `requests` stay as hidden aliases.
   - **Implemented:** after checking `/visura/history?status=...` on the active CLI service, remove the client-side
     `status` re-filter so the service applies it before pagination.
2. `db migrate | status | backfill`.
   - `init` becomes a hidden alias of `migrate`.
   - `db status` also prints `DATABASE_REVISION` and whether the database is behind it (alembic `current` vs `heads`).
   - `db.py` already finds `alembic.ini` through `_runtime.project_root()` in this checkout. Keep that behavior and
     add coverage for a non-editable install; do not reintroduce a path relative to the package.
3. `floor-plan-validate` → `documents floor-plan-validate` (alias kept).
4. Language: decide once (§8, decision 1) and apply it to help and messages in `sister/cli/`. **Implemented:** CLI-owned
   workflow descriptions and validation errors are Italian. The forwarded tools follow in Phase 7 as they are ported.
5. Root help: group the commands by what they need (Typer `rich_help_panel`: "Service", "Database", "Local files",
   "Checkout tools") and list the exit codes.

Done when: `sister --help` shows the tree of §3.3 with panels, and every alias has a test that checks the
deprecation line and that the new command is called.

### Phase 7 — Port the forwarded tools

Order (most used first, from the batch/session workflow in `CLAUDE.md`):

1. **Implemented:** `batch run-session` (`run_in_session.py`), `batch progress` (`batch_progress.py`)
2. **Implemented:** `batch build-list`, `batch prune-list`, `batch convert-list`, `batch convert-pvp-csv`
3. **Implemented:** `app login` (`ade_login.py`), `app hash-password`, `app start`, and `app stop`
4. **Implemented:** `documents normalize`, `documents reconcile`, `documents cleanup-xml`, `documents rename-date`, `documents import`
5. **Implemented:** `build dossier-graph`, `build building-kmz`
6. **Implemented:** `batch build-pdf-parseable`, `batch recrowd`, `documents import-ocular|smart-ocr|download-missing-p7m|define-workflow`, `audit *`

For each tool:

- Split the module into `run(...)` (the logic, plain keyword arguments, returns a result or exit code) and the
  argparse `main()`. Keep `main()` so `python -m sister.command_tools.<x>` keeps working for scripts and cron.
- Add a Typer command in the matching `sister/cli/<group>.py` that calls `run(...)` with the same options. Keep
  option names; where they differ from the shared conventions, add the shared name and keep the old one hidden.
  - `--dry-run` is the default for destructive tools and `--apply` performs. This already holds for `normalize`,
    `reconcile`, `cleanup-xml` and `rename-date`; check each one and make it hold.
  - `-o/--output` always means "write the result JSON here".
- Add the tool to the help snapshot (`tests/fixtures/cli_tree.json`). The forwarding registry (`tools.py`) is gone.

Shell helpers:

- **Implemented:** `run-list` is a separate `batch run-list` Python coordinator over `build-list` and `run-session`
  (§3.4). It records the login time, chooses a batch folder (resume or new), uses a temporary folder for `--dry-run`,
  and passes the list transforms to `build-list`. Session setup, deadline, service cleanup and document download stay
  in `run-session`; per-row work stays in the batch engine behind `query batch`. `run-list` does not add a second
  submission/retry/journal implementation or absorb `query batch`'s generic options. The old shell script is now a thin
  wrapper around `sister batch run-list`.
- `run-session` keeps starting `query batch` as a subprocess (§3.4). Porting `run_in_session.py` to Typer changes its
  entry point only, not that call.
- **Implemented:** `run-pvp-is` and `run-pdf-parseable` are Python commands that call the shared list/batch runners;
  their shell files are thin CLI wrappers.
- **Implemented:** `app start` wraps `scripts/start.sh` (uvicorn with reload flags) and accepts `--no-reload` and the port
  as real Typer options.
- **Implemented:** `app stop` uses `sister/cli/app_tools.py` to replace `../bin/kill_app.sh` (find the listener under
  `/proc`, require `sister.main` in its command line, send SIGTERM, wait, and send SIGKILL after a timeout). It refuses
  non-SISTER listeners and requires `--pid` if more than one SISTER process matches.
- `dev fixture-payloads` can stay a shell wrapper (developer only).

Done when: every operational command has a Typer entry point; `app start` delegates only the Uvicorn launch to
`scripts/start.sh`, compatibility shell scripts call the CLI, and `dev fixture-payloads` remains the developer shell
command; and `sister batch run-session --help` plus the §6 internal-command contract pass.

## 5. Conventions the plan fixes

| Topic | Rule |
|---|---|
| Option meaning | `--yes/--no` = download again or reuse an answered free query, nothing else. Paid steps: `--confirm-paid`, only where the server accepts it. `--force` = bypass reuse. `--dry-run` = send/change nothing. `--apply` = perform a destructive local change. `-o/--output` = write JSON to a file. `--json` (root) = JSON on stdout. |
| Short options | Kept as today for the cadastral fields (`-P -C -F -p -t`); no new short option that clashes with them. `-sub` (two letters after one dash) is replaced by `--subalterno`/`--sub`. **Implemented** for the generated query commands: `-sub` is a hidden option (`LEGACY_FLAGS` in `query_forms.py`) that fills the same parameter and prints a deprecation line on stderr; the hidden `query workflow` alias keeps `-sub` as it was. |
| Output streams | Data on stdout; progress, hints and warnings on stderr. |
| Imports | A group module imports at the top only `typer`, `rich`, `_runtime` and the light modules it needs (`query_forms`, `client`); everything else inside the command. |
| New single-step query | Only in `query_forms.py` (unchanged rule; now it covers paid queries too). |
| New command group | A module in `sister/cli/`, mounted in `_commands.py`, with a `rich_help_panel` saying what it needs. |

## 6. Testing

- **Baseline first.** Record the current result of
  `../.venv/bin/python -m pytest -p no:logfire --continue-on-collection-errors tests/test_cli.py tests/test_cli_commands.py tests/test_batch.py tests/test_query_forms.py tests/test_workflows.py`
  before Phase 0. Every phase keeps at least the same passes.
- **Help snapshot.** A test walks the Typer app (`typer.main.get_command(app)`) and compares
  `command path → params (name, flags, required, hidden)` with `tests/fixtures/cli_tree.json`. Update that fixture
  deliberately when changing the CLI surface so each command and option change shows in the diff. A separate test
  pins the internal API of §3.4 (`query batch -I --resume --output-dir -o --stop-at`,
  `batch run-list` routing and its forwarded `--extra` / `--stop-stale-service` options, `query soggetto --cf --wait
  --yes -o`, `python -m sister.cli` resolving): those may not change even on purpose without updating the relevant
  caller in the same PR. The tree must contain `batch run-list` and must not contain `query run-list`.
- **Aliases.** One parametrized test: each deprecated name prints the deprecation line on stderr and calls the same
  function as the new name.
- **JSON mode.** One parametrized test over the data-printing commands with a faked `VisuraClient`: stdout parses as
  JSON, stderr may have text.
- **Renderers.** Unit tests on `render.py` with `Console(record=True)`, no CLI runner needed.
- **Ported tools.** Test `run(...)` directly; one `CliRunner` test per command for option wiring.
- **Live checks** (cannot be unit tested; do them after Phases 2, 5 and 7, after SISTER login):
  - `sister health`;
  - `sister query search ... --richiedi-documenti false --wait`;
  - `sister workflow run --preset due-diligence ... --depth light`;
  - `sister batch run-list` on a small Markdown input, then `sister batch run-session` on its plan;
  - the resulting `sister query batch` execution, plus one direct `query batch` run from a prepared small plan.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Scripts, cron jobs and docs call the old names | Hidden aliases for one release with a stderr warning; grep `scripts/`, `docs/`, `CLAUDE.md`, `inputs/` and crontab for old names in the same PR that adds each alias, and update them. |
| Phase 2's single event loop changes timing in long `--wait` runs | Only the connection pool changes; polling (`VISURA_LONG_POLL`) doesn't. Run the live checks of §6 before merging. |
| A paid query becomes reachable by a new path in Phase 4 (batch, MCP) | `paid=True` is refused by `batch.py` and MCP `submit_query`, with tests for both; batch has no paid-query opt-in. |
| The `custom` workflow preset differs from the deleted client-side flow | A direct differential check can no longer run because the old flow is gone. `test_workflows.py::test_custom_workflow_matches_legacy_client_start_shapes` pins the recorded legacy start sequences for CF, P.IVA, address, and parcel with or without `subalterno`; this is a regression expectation, not an executable comparison against the old implementation. |
| `app stop` in Python kills the wrong process | Match on the listening port **and** a `sister.main` command line; refuse non-SISTER listeners and require `--pid` when more than one SISTER listener matches. |
| `run-session` mistakes a running batch for a stale service | This remains a known, unchanged limitation: `stop_stale_service()` matches the text `sister.cli query batch` in the process list. The check has only been reviewed locally; no authenticated `run-session` check has exercised this collision. Keep the subprocess call (§3.4), or replace text matching with an unambiguous ownership signal (for example a lock file in the batch folder) before relying on stale-service cleanup during a live run. |
| Phase 1 breaks `python -m sister.cli` | `sister/cli/__main__.py` is part of Phase 1; the internal-API test of §6 runs `python -m sister.cli --help`. |
| Moving `cli.py` loses `git blame` | `git mv` first in its own commit, then edit; use `git log --follow`. |

## 8. Recorded decisions

1. **Language of help and messages.** Use Italian for CLI-owned help, validation and user-facing messages; keep
   technical identifiers, statuses and JSON keys unchanged. Forwarded command surfaces follow the same rule.
2. **Alias lifetime.** Keep hidden deprecated aliases for one tagged release, then remove them in a separate PR.
3. **Paid queries in batch files.** Keep paid forms out of batch schemas, plans and MCP submission. There is no
   `allow_paid` opt-in.
4. **`mortgage` group.** Keep `mortgage status|quote`; `mortgage start` remains a deprecated alias for the free
   `query ipotecaria-persona-fisica` preview.

## 9. Size and order

| Phase | Size | Depends on |
|---|---|---|
| 0 Safety fix | S (½ day) | — |
| 1 Package split | M (1-2 days) | — |
| 2 Runtime and renderers | M (1-2 days) | 1 |
| 3 JSON output | S (1 day) | 2 |
| 4 One definition per query | M (2 days) | 2 |
| 5 One workflow engine | M-L (2-3 days, server + CLI) | 2 |
| 6 Tree cleanup | S-M (1-2 days) | 4, 5 |
| 7 Port tools | L (1-2 days per group, 6 groups) | 2; can run in parallel with 3-6 |

Phase 0 can merge on its own today. Phases 4, 5 and 7 are independent of each other once 2 is in.
