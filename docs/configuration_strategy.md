# Shared and application configuration

Status: proposed configuration contract. This document defines the target;
the existing entry-point loaders have not yet been replaced. Here, system
configuration means defaults shared by the applications in the workspace.

## Precedence

Resolve each setting using this order, from highest priority to lowest:

| Priority | Source | Responsibility |
| --- | --- | --- |
| 1 | Process environment | Shell overrides, CI, service managers, deployed secrets |
| 2 | Application `.env` | Settings and overrides for this application |
| 3 | Workspace `../.env` | Common defaults and deliberately shared service configuration |
| 4 | Code defaults | Safe, documented defaults for optional settings |

An explicit command option overrides its corresponding setting for that command
only. It does not modify either file or the parent process environment.

Determine the application root explicitly. For SISTER it is the directory
containing `pyproject.toml` and `sister/`. Read exactly `<app_root>/.env` and
`<app_root>/../.env`, independently of the current working directory. Do not
search arbitrary ancestors or load a sibling application's `.env` implicitly.

Missing files are allowed; missing required settings fail validation. An existing
but unreadable file is an error. `.env.example` documents settings and is never
loaded as a runtime source.

## Ownership and naming

Keep common defaults in `../.env`: logging, the default runtime environment,
workspace paths, and service configuration intentionally shared by applications.
Keep SISTER database URLs, portal credentials, application identity, authentication
secrets, and SISTER-specific paths in SISTER's `.env` or its deployed environment.

Use `SISTER_` for new application-owned keys, especially when a setting must
appear in a shared source. Keep existing shared-package contracts such as
`ENVIRONMENT`, `LOG_LEVEL`, and `EMAIL_PROVIDER`; the application can override the
same key in its `.env`. Do not introduce a second prefixed alias for every
existing shared setting.

For example, the proposed workspace file could contain:

```dotenv
# ../.env: inherited by sibling applications
ENVIRONMENT=development
LOG_LEVEL=INFO
GCP_PROJECT_ID=apps-aecs4u
AECS4U_DATA_ROOT=/data/aecs4u.it
```

The proposed SISTER file could contain:

```dotenv
# sister/.env: overrides workspace defaults for SISTER only
LOG_LEVEL=DEBUG
SISTER_OUTPUTS_DIR=${AECS4U_DATA_ROOT}/sister
DATABASE_SCHEMA=sister
SISTER_DATABASE_DSN_LOCALHOST=postgresql+psycopg://postgres@127.0.0.1:5432/sister?options=-csearch_path%3Dsister
# Supply a real Neon URL through this file or the deployed process environment:
# SISTER_DATABASE_DSN_NEON=postgresql+psycopg://user:password@host/sister?sslmode=require&options=-csearch_path%3Dsister
```

For `LOG_LEVEL`, SISTER uses `DEBUG`; another application without an override
uses `INFO`. Starting SISTER with `LOG_LEVEL=WARNING` uses `WARNING` without
editing a file. The interpolation example above is part of the proposed contract;
the current independent loaders do not guarantee that behavior.

Do not place app-owned secrets in `../.env` merely to avoid duplication. Sharing a
file is an explicit choice to make its settings available to each app loading it.

## Runtime environment and database selection

Merge all configuration sources first, then select the database using the
effective `ENVIRONMENT` value:

| Effective environment | Required database setting |
| --- | --- |
| `development`, `local`, `localhost`, `test`, `testing` | `SISTER_DATABASE_DSN_LOCALHOST` |
| `production`, `prod`, `staging` | `SISTER_DATABASE_DSN_NEON` |

An absent environment defaults to `development`. The target validator should
reject unrecognized or empty environment names, so a spelling error cannot change
the database selection. Currently the selector maps other names to localhost;
strict environment validation is a proposed follow-up.

The selected database URL must exist and use PostgreSQL. Never fall back to the
other URL or the retired `DATABASE_DSN` environment variable. Both URLs can exist
in a local file without changing which one is selected. Database tests always use
the explicit localhost setting and isolated schemas, regardless of the runtime
environment.

In Cloud Run, inject `ENVIRONMENT=production` and `SISTER_DATABASE_DSN_NEON`.
The container does not need either `.env` file. Keep the existing secret-manager
resource name independent of the application environment-variable name.

## Loading and validation

Use one loader and one immutable settings snapshot for each application process:

1. Capture the original process environment before loading files or importing
   modules that read configuration.
2. Parse the two explicit files into dictionaries without modifying `os.environ`
   and without interpolating values yet.
3. Merge workspace, application, and original process values in that order,
   preserving a source label for each winning key.
4. Expand `${NAME}` references in winning file values against the final merged
   mapping. Resolve dependency chains and reject undefined references or cycles.
   Treat values injected by the process environment literally, including secrets.
   Resolve references only for settings consumed by the app and their dependencies,
   so unused workspace settings do not block startup.
5. Validate known settings and compute derived values, including database selection.
   An explicit empty value overrides lower layers; an empty required setting
   fails instead of inheriting an older value. A bare `KEY` without `=` is an error.
6. Expose typed settings to SISTER. Export resolved file values once for existing
   dependencies that still read `os.environ`, preserving original process values.

Use `dotenv_values(..., interpolate=False)` for parsing; expansion and validation
are deliberate parts of this proposed loader. Import-time reads and repeated
`load_dotenv()` calls must not become competing configuration sources. Restart
the process to apply file edits. Tests can construct settings from temporary
files and explicit environment mappings without rewriting real configuration.

For several applications within one Python process, pass each app's settings to
shared components explicitly; process-global environment mutation cannot provide
different values for the same key to each app.

Keep source diagnostics separate from secret values. A planned configuration
inspection command should report, for example:

```text
ENVIRONMENT                    workspace file
LOG_LEVEL                      application file
SISTER_DATABASE_DSN_LOCALHOST   application file [configured]
SISTER_DATABASE_DSN_NEON        process environment [configured]
active database setting        SISTER_DATABASE_DSN_LOCALHOST
```

## Entry points and deployment

The API, CLI, MCP server, Alembic, and standalone scripts must all initialize the
same loader before importing consumers. The shared launcher must apply the same
contract before invoking tools that need bootstrap settings such as
`UV_CACHE_DIR`; loading them only inside Python is too late for those tools.
Use dotenv parsing rather than evaluating files with shell `source`.

Python dependencies that create their own settings objects must consume the
resolved environment or explicit settings. Check their independent `env_file`
behavior so an app-level blank override cannot reveal a lower-priority value.

Docker Compose resolves container variables before application startup. Its
`environment` entries and explicit CLI overrides take precedence over service
`env_file` values; host variables are only passed when the Compose configuration
forwards them. A shared and app file can be supplied in that order through
service `env_file`, with the app file overriding the shared one. Once injected,
these are process values from SISTER's perspective. Verify interpolation separately:
Compose does not implement the custom final-merge expansion contract above.
Production deployments should inject the resolved settings directly.

## Changes needed in the current repository

- Replace the separate loaders in `main.py`, `database.py`, `mcp_server.py`,
  `cli.py`, `alembic/env.py`, command tools, and scripts with the common bootstrap.
- Replace implicit dotenv searches with explicit application-root paths.
- Correct Jev's shared path: it currently uses `project_root.parents[1]`, which
  points above the intended workspace file; use `project_root.parent`.
- Route the OCR tool's optional Ocular settings through an explicit integration
  configuration instead of importing another application's entire environment.
- Keep authentication/email adapters on the same resolved settings snapshot.
- Document bootstrap behavior for the shared launcher and Compose without changing
  secrets or relocating existing credentials as part of the loader refactor.

Acceptance checks: verify each precedence boundary; selection when both DSNs
exist; missing/empty required settings; interpolation after overrides; missing
references/cycles; independence from working directory and import order; identical
results across API/CLI/Alembic; and diagnostics that never print secret values.

## References

- [python-dotenv: parsing dictionaries and environment precedence](https://bbc2.github.io/python-dotenv/).
- [Docker Compose: environment-variable precedence](https://docs.docker.com/compose/how-tos/environment-variables/envvars-precedence/).
- [Docker Compose: service environment files](https://docs.docker.com/reference/compose-file/services/#env_file).
