# Run a Markdown list in the current SISTER session

`sister batch run-list` converts a supported Markdown list into a batch plan, then runs that plan through the
SISTER browser session already open in shared Chrome. The runner starts and stops its own SISTER service process; you
do not need to start `scripts/start.sh` separately.

## Before you start

1. Make sure shared Chrome is running.
2. Log in to SISTER and leave the authenticated session open. For example:

   ```bash
   uv run sister app login
   ```

   Complete the SPID/CIE step, including any phone approval. Run the list command soon after login because the portal
   session lasts about 30 minutes.
3. Use a Markdown batch file ([format](batch_markdown_format.md)): tables marked `<!-- sister-batch-table -->` with one
   query per row, in any mix of commands (subjects, parcels, PVP lists such as `inputs/pvp_sales/IS.md`, or the legacy
   `inputs/codici_fiscali.all.md`). Remove what SISTER already has first with `sister batch prune-list FILE.md`.

See [batch format](batch_format.md) for the underlying batch behavior and [portal session](portal_session.md) for
login and session details.

## Run a list

From the repository root, pass the Markdown file path as the first argument:

```bash
uv run sister batch run-list inputs/codici_fiscali.all.md
```

To verify the portal session before starting a large batch, pass a valid codice fiscale for a subject-search probe:

```bash
uv run sister batch run-list --preflight-cf CF_DI_VERIFICA inputs/codici_fiscali.all.md
```

This performs one real SISTER search and stops before the batch if the search fails. Use a verification code that is
not also in the input list, since the preflight is separate from the batch. The same option is available through the
`SISTER_PREFLIGHT_CF` environment variable. Without either setting, `run-list` only confirms that the service can
attach to the open browser; it does not prove that SISTER will accept a search.

To receive a progress summary when the session run ends, enable email delivery:

```bash
uv run sister batch run-list --email-progress inputs/codici_fiscali.all.md
```

The message is sent through `aecs4u-email` after the runner stops its service. It includes final batch totals and the
number of additional responses completed during that run. The default recipient is `demo@aecs4u.com`; override it with
`--email-to ADDRESS`. Email provider settings come from the repository's normal `aecs4u-email` environment config.

For a PVP parcel list, for example:

```bash
uv run sister batch run-list inputs/pvp_sales/IS.md
```

The path can be absolute. A relative path is resolved from the directory where you run the command. The script records
the current system time as the login time, so start it just after logging in.

## Batch directory and resume behavior

You can provide a second argument to choose the output directory:

```bash
uv run sister batch run-list inputs/codici_fiscali.all.md outputs/batches/codici_fiscali_run_01
```

The batch directory contains `batch.json`, the progress journal `batch_journal.jsonl`, and run logs. Use the same
directory to resume that batch after an interruption.

If you omit the second argument, the script looks under `outputs/batches/` for the latest journal whose directory name
starts with the Markdown file's basename. It resumes that batch when found; otherwise it creates a new timestamped
directory. Lists with the same basename share this automatic lookup, even if they live in different source folders.
Pass a new, unique directory when you want to start a separate run instead of resuming an existing one.

The batch runner skips answers already present in the database and resumes incomplete rows from its journal. On normal
completion, the session deadline, or Ctrl-C, it also attempts to import ready documents from SISTER's **Richieste**
page, skipping documents already stored. A Richieste download failure is reported in the output and does not discard
the batch results.

## Python runner

The script chooses a Python command in this order:

1. `PYTHON_RUNNER`, if set;
2. the repository's `.venv/bin/python`, if present;
3. `uv run --no-project python`, if `uv` is installed.

To select a runner explicitly, set `PYTHON_RUNNER` for the command:

```bash
PYTHON_RUNNER="uv run --no-project python" uv run sister batch run-list inputs/codici_fiscali.all.md
```

## What to expect

- The batch runs sequentially in the active SISTER session and stops at its configured deadline, before the portal's
  approximate session expiry.
- The terminal shows batch progress. The final summary reports completed, pending, and failed rows.
- `--email-progress` sends the final session progress by email after the run ends; this is opt-in.
- Generated files and logs remain in the selected batch directory under `outputs/batches/` by default.
- To continue after a stop or interrupted run, log in again and rerun the same command (or pass the same batch
  directory explicitly).

For the PVP `IS.md` list, `sister batch run-pvp-is` is an optional wrapper that prompts for the Markdown path and
then calls `sister batch run-list`.

## Help

The shell runners accept `-h` or `--help`. The Python scripts use `argparse`, which provides the same options:

```bash
uv run sister batch run-list --help
uv run sister batch run-pvp-is -h
uv run sister batch convert-pvp-csv --help
uv run sister batch build-list --help
uv run sister batch run-session --help
```
