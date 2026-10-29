# Run a Markdown list in the current SISTER session

`scripts/run_list_in_session.sh` converts a supported Markdown list into a batch plan, then runs that plan through the
SISTER browser session already open in shared Chrome. The runner starts and stops its own SISTER service process; you
do not need to start `scripts/start.sh` separately.

## Before you start

1. Make sure shared Chrome is running.
2. Log in to SISTER and leave the authenticated session open. For example:

   ```bash
   ../.venv/bin/python scripts/ade_login.py
   ```

   Complete the SPID/CIE step, including any phone approval. Run the list command soon after login because the portal
   session lasts about 30 minutes.
3. Use a list file in one of the formats supported by `scripts/build_batch_from_list.py`:
   - tax-code lists such as `inputs/codici_fiscali.all.md`;
   - parcel query tables marked `<!-- sister-batch-table:v1 -->`, such as the Markdown generated from PVP CSVs.

See [batch format](batch_format.md) for the underlying batch behavior and [portal session](portal_session.md) for
login and session details.

## Run a list

From the repository root, pass the Markdown file path as the first argument:

```bash
./scripts/run_list_in_session.sh inputs/codici_fiscali.all.md
```

For a PVP parcel list, for example:

```bash
./scripts/run_list_in_session.sh inputs/pvp_sales/IS.md
```

The path can be absolute. A relative path is resolved from the directory where you run the command. The script records
the current system time as the login time, so start it just after logging in.

## Batch directory and resume behavior

You can provide a second argument to choose the output directory:

```bash
./scripts/run_list_in_session.sh inputs/codici_fiscali.all.md outputs/batches/codici_fiscali_run_01
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
3. `uv run --no-project`, if `uv` is installed.

To select a runner explicitly, set `PYTHON_RUNNER` for the command:

```bash
PYTHON_RUNNER="uv run --no-project" ./scripts/run_list_in_session.sh inputs/codici_fiscali.all.md
```

## What to expect

- The batch runs sequentially in the active SISTER session and stops at its configured deadline, before the portal's
  approximate session expiry.
- The terminal shows batch progress. The final summary reports completed, pending, and failed rows.
- Generated files and logs remain in the selected batch directory under `outputs/batches/` by default.
- To continue after a stop or interrupted run, log in again and rerun the same command (or pass the same batch
  directory explicitly).

For the PVP `IS.md` list, `scripts/run_pvp_sales_is.sh` is an optional wrapper that prompts for the Markdown path and
then calls `scripts/run_list_in_session.sh`.

## Help

The shell runners accept `-h` or `--help`. The Python scripts use `argparse`, which provides the same options:

```bash
./scripts/run_list_in_session.sh --help
./scripts/run_pvp_sales_is.sh -h
./.venv/bin/python scripts/convert_pvp_sales_csvs_to_markdown.py --help
./.venv/bin/python scripts/build_batch_from_list.py --help
./.venv/bin/python scripts/run_in_session.py --help
```
