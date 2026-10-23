# Batch submission file (`sister query batch`)

`sister query batch` runs many single-step queries (the commands of `sister/query_forms.py`) from one file.
The preferred format is **JSON**, described here; the older **CSV** is still read and converted to the same
structure (see the end).

* Schema (editors, validators): [`batch_submission.schema.json`](batch_submission.schema.json), regenerated with
  `sister query batch-schema -o docs/batch_submission.schema.json` (a test fails when it drifts from the code). It lists
  the commands and, per command, the allowed `params`.
* Complete example: [`examples/batch_example.json`](examples/batch_example.json).
* Code: `sister/batch.py` (format models, planner, runner, journal).

## Structure

```json
{
  "$schema": "./batch_submission.schema.json",
  "version": 1,
  "name": "Due diligence Trieste",
  "description": "free text",
  "defaults": { "command": "search", "params": { "tipo_catasto": "F" } },
  "options": { "wait": true, "reuse": "ask", "max_in_flight": 1, "retries": 1, "retry_delay": 5,
               "poll_timeout": 900, "on_error": "continue", "output_dir": "outputs/run1" },
  "queries": [
    { "id": "ts-9-166",
      "params": { "provincia": "Trieste", "comune": "TRIESTE", "foglio": 9, "particella": 166 },
      "metadata": { "source_files": "vi_att_TS_FG9_PT166.pdf" } },
    { "command": "soggetto", "params": { "codice_fiscale": "RSSMRA85M01H501Z", "tipo_catasto": null } }
  ]
}
```

Unknown keys are rejected everywhere (a typo is an error, not an ignored setting). Only `queries` is required.

### Top level

| Key | Type | Meaning |
|---|---|---|
| `$schema` | string | optional, for editor completion |
| `version` | `1` | format version (default 1) |
| `name`, `description` | string | labels; `name` is copied to the report |
| `defaults` | object | values every query starts from |
| `options` | object | how the batch runs |
| `queries` | array, ≥ 1 | the queries, executed in this order |

### `defaults`

| Key | Meaning |
|---|---|
| `command` | command of the queries that do not name one (default `search`) |
| `params` | parameters merged **under** each query's own; `null` in a query removes an inherited value |

### `queries[]`

| Key | Default | Meaning |
|---|---|---|
| `id` | `q0001`, `q0002`, … | unique, `[A-Za-z0-9._-]`, max 64 chars. The journal and `--resume` key on it: give stable ids to a file you will edit and rerun |
| `command` | `defaults.command` | any `sister query` command or alias (`sister queries`; `persona-giuridica` = `azienda`) |
| `params` | `{}` | the command's parameters, **same names as its CLI options** / form inputs (`sister query <command> --help`). Strings, numbers and booleans are accepted (`"foglio": 9`, `"con_intestati": true`) and sent as text |
| `enabled` | `true` | `false` keeps the query in the file but does not run it |
| `reuse` | `options.reuse` | per-query override |
| `retries` | `options.retries` | per-query override |
| `metadata` | `{}` | free notes copied to the result (never sent to SISTER). `source_files` and `scope_note` also appear at the top level of the result, as in the CSV batches |

### `options`

Every option can be overridden by a CLI flag (the flag wins).

| Key | Default | CLI | Meaning |
|---|---|---|---|
| `wait` | `false` | `--wait/--no-wait` | wait for each result; otherwise rows are only submitted (status `queued`) |
| `reuse` | `ask` | `--yes` = `force`, `--no` = `reuse`, `--force` = `force` | an identical query already answered: `ask` reuses it and asks before downloading a **stale** answer again (no terminal = no); `reuse` always reuses; `force` always sends a new, possibly paid, request |
| `max_in_flight` | `1` | `--max-in-flight` | rows handled at once (1–8). The service drives one portal session, so values above 1 only keep its queue full; CAPTCHA pages are still one at a time |
| `retries` | `1` | `--retries` | extra attempts (0–5) for a row that fails with a transient error (connection, HTTP 5xx/429) or a result with status `error`; waits `retry_delay`, then doubles. A retry is a new request (`force`). HTTP 4xx, `needs_human` and `timeout` are never retried |
| `retry_delay` | `5` | – | seconds before the first retry |
| `poll_interval` | client default | – | seconds between polls |
| `poll_timeout` | client default (300 s) | `--poll-timeout` | seconds to wait for one request. A row that needs a CAPTCHA needs time for a human to type it |
| `on_error` | `continue` | `--on-error` | `stop` = after the first failed row the others are not started (status `skipped`, still pending for `--resume`) |
| `output_dir` | none | `-O/--output-dir` | one `batch_NNNN_<command>.json` per waited row, and the journal |

## What happens when it runs

1. **Validation of the whole file first.** Unknown command, parameter that the command does not have, missing required
   parameter, invalid choice: all problems are listed and **nothing is sent** (exit 2). `--skip-invalid` runs the valid
   rows anyway; `--dry-run` prints the plan (and the problems) without sending.
2. **Duplicates** (same command and parameters after merging defaults) run once; the repeat is reported as `duplicate`.
3. Rows are submitted in file order (`max_in_flight` at a time), each with the reuse policy above.
4. **Journal** (`<output_dir>/batch_journal.jsonl`, or `--journal FILE`): one JSON line per state change (submitted,
   finished), written immediately. A crash, a service reload or Ctrl-C loses nothing already sent.
5. **`--resume`** reads the journal: `completed` rows are skipped; rows submitted but not finished (`queued`,
   `timeout`) are **waited for again with their old request ids, never submitted again** (a repeated request may be
   paid twice); `error`/`needs_human` rows are submitted again as new requests; a row whose command or parameters
   changed since is a new request. Without `--resume` the journal is only appended to.

Exit code: 0 when every row is `completed` or `queued` (or intentionally not run: `duplicate`, `disabled`,
`skipped`); 1 when any row failed or needs a human; 2 for an invalid file or options.

## Result statuses

| Status | Meaning |
|---|---|
| `completed` | all requests of the row finished |
| `queued` | submitted, not waited for (`wait: false`) |
| `error` | failed after `retries` attempts (`error` has the reason) |
| `needs_human` | a CAPTCHA nobody solved; type it in the SISTER tab and `--resume` |
| `timeout` | still running when polling stopped; `--resume` polls it again |
| `invalid` | failed validation (`error` says why) |
| `duplicate`, `disabled`, `skipped` | not run |

## Output

`-O DIR` writes `batch_NNNN_<command>.json` (NNNN = position in the file) for each waited row:
`{row, id, command, status, label, request_ids, results: {<request_id>: <service response>}, attempts, metadata, …}`.

`-o FILE` writes the report: `{name, started_at, finished_at, total, counts: {status: n}, results: [row entries]}`
(earlier versions wrote a bare list of the entries).

## Caution

The service reloads when any `*.py` changes (see `CLAUDE.md`); that drops the request in flight, and `--resume`
exists for it. Paid steps (documents, ispezioni ipotecarie) are not gated by the batch: use `reuse: "ask"` or
`"reuse"` and review `--dry-run` before running a file that contains them.

## CSV (still supported)

`--input file.csv`: the columns are the parameters of the command (`#` lines are comments), the optional `command`
column picks each row's command (else `--command`, default `search`), an optional `id` column names the row, and any
column that is not a parameter of the row's command is kept as `metadata` (`source_files`, `scope_note`, `vista`, …).
Same validation, journal and resume as JSON. Two behaviour changes from the first CSV batch: an invalid row now stops
the run before anything is sent (it used to be reported while the rest ran; `--skip-invalid` restores that), and the
exit code is 1 when a row failed.
