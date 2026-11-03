# Scripts and operational commands

Repeatable app operations, batch workflows, document maintenance, exports, and UI audits are exposed through the installed
`sister` CLI. Run commands from the repository root; use `sister --help` or append `--help` to a command for its options.
The implementation modules live in [`sister/command_tools/`](../sister/command_tools/) and are invoked through
[`sister/cli.py`](../sister/cli.py).

```bash
uv run sister --help
uv run sister batch build-list --help
uv run sister documents normalize --help
```

Command groups:

- `app`: `login`, `start`, `stop`, `hash-password`
- `batch`: `build-list`, `convert-pvp-csv`, `build-pdf-parseable`, `run-session`, `run-list`, `run-pvp-is`,
  `run-pdf-parseable`, `progress`, `prune-list`, `recrowd`
- `documents`: `define-workflow`, `import`, `import-ocular`, `smart-ocr`, `download-missing-p7m`, `rename-date`,
  `normalize`, `cleanup-xml`, `reconcile`
- `build`: `dossier-graph`, `building-kmz`
- `audit`: `sweep`, `interactions`, `demo-purchases`, `purchase-paths`
- `dev`: `fixture-payloads`

Recrowd's two historical-visura workflows share one command. Select which collection to process with `--items`:

```bash
uv run sister batch recrowd --items proponents --limit 2
uv run sister batch recrowd --items properties --limit-proponents 2 --limit-properties 5
```

Both commands are read-only by default. Add `--execute` to submit missing requests. The proponents workflow also supports
`--download-ready`; the properties workflow uses the two separate limit options shown above.

`start.sh`, `run_list_in_session.sh`, `run_pdf_parseable_batch.sh`, `run_pvp_sales_is.sh`, and
`sister_fixture_payload_commands.sh` remain shell wrappers in this folder and call the unified CLI where applicable.
`kill_app.sh [PORT]` stops the listener on the selected port (default 8025).

## One-off scripts

Use `one_off/` for fixed-data backfills, targeted repairs, and scripts tied to a particular input set.

| Script | Purpose |
| --- | --- |
| `one_off/backfill_db.py` | Backfill request and result tables from existing output JSON files. |
| `one_off/backfill_reports.py` | Import the legacy reports archive described in the script. |
| `one_off/batch_opendata.py` | Submit the fixed OpenData cadastral request set. |
| `one_off/download_remaining.py` | Continue the specific remaining-code-list workflow. |
| `one_off/omi_quote_index.sql` | Install the OMI quote indexes concurrently. |
| `one_off/populate_query_data.py` | Import exported query JSON into the configured database. |
| `one_off/query_recrowd_proponents.py` | Query the fixed Recrowd proponent set. |
| `one_off/repair_subject_data.py` | Apply or revert the targeted subject-data repair plan. |
