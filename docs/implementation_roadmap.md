# Implementation Roadmap

This document turns the current improvement backlog into an execution plan for the
next development phases of SISTER. It is intentionally pragmatic: each phase has
clear goals, boundaries, and acceptance criteria so work can be delivered in
small, reviewable increments.

## Context

The app is now a combined API, CLI, Web UI, and workflow engine. That is useful,
but it also means a few modules have become structural bottlenecks:

- [sister/utils.py](/mnt/developer/git/aecs4u.it/visura-api/sister/utils.py)
- [sister/workflows.py](/mnt/developer/git/aecs4u.it/visura-api/sister/workflows.py)
- [sister/cli.py](/mnt/developer/git/aecs4u.it/visura-api/sister/cli.py)
- [sister/static/js/sister_forms.js](/mnt/developer/git/aecs4u.it/visura-api/sister/static/js/sister_forms.js)
- [sister/main.py](/mnt/developer/git/aecs4u.it/visura-api/sister/main.py)
- [sister/web.py](/mnt/developer/git/aecs4u.it/visura-api/sister/web.py)

The next round of work should optimize for maintainability, operator visibility,
and controlled growth rather than adding more raw feature surface.

## Guiding Principles

- Keep business logic in shared Python modules, not duplicated across CLI, API, and Web UI.
- Prefer small, reversible refactors over large rewrites.
- Add tests before or alongside structural changes.
- Treat workflow runs as first-class operational objects.
- Do not introduce Prefect until the current internal workflow boundaries are cleaner.

## Phase 0: Baseline And Guardrails

### Goal

Reduce change risk before deeper refactors.

### Scope

- Add missing browser and web-route regression coverage for `/web/forms`, workflow submission, and polling.
- Add workflow-level timing and queue telemetry.
- Add a lightweight architecture note describing current module responsibilities.

### Concrete changes

- Add tests for:
  - `/web/forms`
  - `/web/api/{endpoint}`
  - `/web/api/visura/{request_id}`
  - batch form submission and workflow submission paths
- Add structured timing logs for:
  - queue wait time
  - step duration
  - full workflow duration
  - cache hit vs fresh execution
- Document current boundaries in `docs/`.

### Acceptance criteria

- Web UI submission paths have automated coverage.
- Workflow and queue timing appear in logs in a machine-readable format.
- The current architecture is documented well enough to support refactoring PRs.

## Phase 1: Remove Web Self-Proxying And Simplify App Wiring

### Goal

Reduce avoidable indirection and make the web layer easier to test.

### Scope

- Remove localhost HTTP self-calls from [sister/web.py](/mnt/developer/git/aecs4u.it/visura-api/sister/web.py).
- Introduce shared service-entry functions for web, API, and CLI.
- Replace monolithic app bootstrap in [sister/main.py](/mnt/developer/git/aecs4u.it/visura-api/sister/main.py) with a cleaner app factory layout.

### Concrete changes

- Replace the current internal HTTP proxy calls with direct calls into:
  - route handlers
  - service-layer functions
  - workflow execution helpers
- Extract from `main.py` into dedicated modules:
  - app factory
  - logging setup
  - auth/theme setup
  - dependency wiring
- Keep public API paths stable.

### Acceptance criteria

- `/web` no longer depends on `http://localhost` calls to the same app.
- The app can be instantiated from a `create_app()`-style factory.
- Web routes can be tested without relying on nested HTTP requests back into the same process.

## Phase 2: Split Workflow And Scraping Monoliths

### Goal

Make the codebase easier to evolve as workflows and enrichment steps grow.

### Scope

- Break up [sister/workflows.py](/mnt/developer/git/aecs4u.it/visura-api/sister/workflows.py).
- Break up [sister/utils.py](/mnt/developer/git/aecs4u.it/visura-api/sister/utils.py).
- Reduce tight coupling between preset definitions, step execution, persistence, and aggregation.

### Concrete changes

- Split workflow code into modules such as:
  - `workflow_registry.py`
  - `workflow_steps.py`
  - `workflow_persistence.py`
  - `workflow_aggregate.py`
  - `workflow_resume.py`
- Split scraping helpers into modules such as:
  - `utils_visure.py`
  - `utils_history.py`
  - `utils_maps.py`
  - `utils_ipotecaria.py`
  - `utils_indirizzo.py`
  - `utils_browser.py`
- Keep public entrypoints stable during the split.

### Acceptance criteria

- No single module remains the default home for unrelated features.
- Adding a new workflow step does not require editing a giant mixed-responsibility file.
- Test coverage remains green throughout the refactor.

## Phase 3: Workflow Run UX And Normalized Result Exploration

### Goal

Make workflows easier to operate and easier to interpret.

### Scope

- Build a dedicated workflow-run UI in the web app.
- Expose aggregated entities, not only raw step payloads.
- Improve result drill-down and resumability from the operator perspective.

### Concrete changes

- Add a workflow run page showing:
  - workflow status
  - step list and per-step status
  - fan-out counts
  - errors and retryable states
  - paid-step visibility
- Add normalized result views for:
  - properties
  - owners
  - links
  - addresses
  - timeline
  - risk flags
- Add resume and rerun actions in the web UI where supported by backend state.

### Acceptance criteria

- An operator can understand workflow progress without inspecting raw JSON.
- A completed workflow can be explored by entity type.
- A failed or interrupted workflow can be resumed from the UI if backend state allows it.

## Phase 4: Execution Controls, Queueing, And Auditability

### Goal

Strengthen operational safety for multi-user and long-running use.

### Scope

- Make execution budgets explicit.
- Improve queue and cancellation controls.
- Add auditability for sensitive or paid operations.

### Concrete changes

- Expose and enforce workflow budgets consistently across CLI, API, and Web UI:
  - `max_hops`
  - `max_owners`
  - `max_properties_per_owner`
  - `max_historical_properties`
  - `max_paid_steps`
  - `max_total_steps`
- Add queue controls:
  - cancellation
  - priority or class-based dispatch
  - visible queue position and wait time
- Add audit logging for:
  - workflow start
  - workflow completion
  - paid-step execution
  - user identity where available

### Acceptance criteria

- Operators can see and control execution budgets.
- Long or expensive workflows are bounded by explicit limits.
- Paid operations are auditable.

## Phase 5: Exports, Reports, And Saved Workflows

### Goal

Turn workflow output into something reusable outside the app.

### Scope

- Add exports and reporting.
- Add saved workflow templates or rerunnable search definitions.

### Concrete changes

- Export completed workflow results as:
  - JSON
  - CSV for normalized entities
  - human-readable HTML or PDF report for due-diligence style runs
- Add saved workflow definitions with prefilled parameters and presets.
- Add summary dashboards for:
  - recent workflows
  - failures
  - high-risk findings

### Acceptance criteria

- Users can reuse workflow definitions without manual re-entry.
- Workflow results can be exported in both machine and human-readable forms.
- Reporting uses normalized aggregate data rather than raw step dumps.

## Phase 6: Optional Prefect Integration

### Goal

Introduce external orchestration only if the app outgrows its current runtime model.

### When this phase becomes justified

- scheduled recurring workflows are required
- many long-running jobs need operational dashboards
- work must be distributed across multiple worker processes or machines
- retry, alerting, and run-history requirements exceed what the current app should own directly

### Recommended boundary

If Prefect is adopted, it should wrap the existing domain workflow engine rather than replace it.

- Prefect manages:
  - scheduling
  - retry policy
  - distributed execution
  - operational visibility
- SISTER keeps ownership of:
  - step definitions
  - preset semantics
  - result aggregation
  - domain persistence
  - browser/session constraints

### Acceptance criteria

- Prefect adds operational value beyond the internal workflow engine.
- The codebase does not duplicate workflow semantics in two orchestration systems.
- The single-browser execution constraint remains explicit and protected.

## Suggested Delivery Order

1. Phase 0
2. Phase 1
3. Phase 2
4. Phase 3
5. Phase 4
6. Phase 5
7. Phase 6 only if operational demands justify it

## First PR Candidates

- Add `/web/forms` interaction tests and workflow timing logs.
- Remove self-proxying from [sister/web.py](/mnt/developer/git/aecs4u.it/visura-api/sister/web.py).
- Extract app startup into a factory and setup modules.
- Split workflow aggregation and persistence out of [sister/workflows.py](/mnt/developer/git/aecs4u.it/visura-api/sister/workflows.py).

## Explicit Non-Goals For The Next Phase

- No full rewrite of the scraping/browser layer.
- No migration to a different framework.
- No Prefect adoption before internal workflow boundaries are cleaner.
- No broad UI redesign before workflow-run UX is implemented.
