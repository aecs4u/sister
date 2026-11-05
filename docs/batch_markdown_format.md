# Markdown batch files (`inputs/*.md`)

A Markdown file is the third way to write the [batch submission](batch_format.md), next to JSON (the canonical format) and
CSV. It loads into the same `BatchFile` model, so validation, `reuse`, journal and `--resume` are the ones of
`sister query batch`. Every list in `inputs/` is read by the same code (`sister/batch_markdown.py`):

| Command | What it does with a Markdown list |
|---|---|
| `sister batch run-list FILE.md` | build the plan and run it in the logged-in session |
| `sister batch build-list FILE.md --output-dir DIR` | write `DIR/batch.json` only |
| `sister batch prune-list FILE.md [--dry-run]` | drop the rows that are already answered/stored |
| `sister batch convert-list FILE.md [-o OUT.md]` | rewrite a legacy codici list in this format |
| `sister query batch --input FILE.md` | run it directly, with all the batch flags |

## Layout

````markdown
# Title                                   ← batch name
First paragraph after the title.          ← batch description (the first paragraph only)

<!-- sister-batch {"options": {"retries": 2}, "defaults": {"params": {"tipo_catasto": "E"}}} -->   (optional)

## Persone fisiche                         ← headings and prose are free text

<!-- sister-batch-table {"command": "soggetto", "params": {"tipo_catasto": "E"}, "metadata": {"tags": "dd"}} -->
| id | codice_fiscale | intestatario | fonte |
|---|---|---|---|
| pf-RSSMRA80A01H501U | `RSSMRA80A01H501U` | Mario Rossi | opendata |

## Immobili

<!-- sister-batch-table -->
| id | command | provincia | comune | foglio | particella | subalterno | tipo_catasto | fonte |
|---|---|---|---|---|---|---|---|---|
| ocr-01-F | visura-storica | BA | Bitetto | 17 | 1824 | 8 | F | PVP 4550689 |
````

* **Document marker** `<!-- sister-batch {...} -->` (single line, optional): JSON with `name`, `description`, `defaults`,
  `options` of the [JSON format](batch_format.md). The title and first paragraph are the fallback for name/description.
* **Table marker** `<!-- sister-batch-table [{...}] -->` before each table. The optional JSON gives what every row of that
  table shares: `command`, `params`, `metadata`. A file can hold any number of tables with different commands and columns;
  `<!-- sister-batch-table:v1 -->` (the PVP files) is the same marker without the JSON, so those files need no change.
* **Rows**: the first line of the table is the header. The cells of a row become, by column name:
  * `command` — the query command (or alias); may be left out when the table marker names one;
  * `id`, `enabled` (`true`/`false`), `reuse`, `retries` — the query fields of the JSON format;
  * a **parameter** of the row's command (`sister query <command> --help`; `codice_fiscale`, `foglio`, …) — empty cells
    are not sent;
  * anything else — **metadata**, copied to the result and never sent (`fonte`, `scope_note`, …); `tags` (comma or
    semicolon separated) are saved on the request records. To keep an informational column whose name is also a
    parameter (`nome`, `sezione`), call it `meta_nome` / `meta_sezione`.
* Cells: `` `code` `` backticks and an em dash (`—`) as "empty" are accepted; a `|` inside a value is written `&#124;`, a
  line break `<br>`. A table ends at the first line that is not a table row; tables without a marker are ignored (report
  tables, notes, report tables).
* **Ids** must be unique in the file and are the key of the journal: do not renumber rows of a list you will rerun.
  Pruning therefore only deletes lines. Without an `id` column rows are `q0001`, `q0002`, … in file order.
* Errors name the line (`riga 7: comando mancante o sconosciuto: 'nope'`) and nothing is sent until the whole file is valid.

## Legacy codici lists

The older `codici_fiscali.all.md` (rows `| 1 | \`CODE\` | Nome | Fonte |` under "Persone fisiche"/"Persone giuridiche") is
still read: a 16-character code with a valid check character is a `soggetto` query, an 11-digit partita IVA an `azienda`
query, both with `tipo_catasto` `E`; the others are reported and skipped. `sister batch convert-list` rewrites such a
file in the table format above (the old `Nome` column becomes `intestatario`, so it can never be sent as the first-name
filter of the person search).

## Pruning (`prune-list`)

Rows are dropped when the answer already exists, with the same definitions as the runner:

* subject rows (`codice_fiscale` / `identificativo`): a Fabbricati+Terreni answer (results or SISTER's "nessuna
  corrispondenza"), "too many results", or a stored subject visura (`answered_codes` in `run_in_session`);
* parcel rows (`comune`, `foglio`, `particella`): a stored visura (`visura`, `visura_fabbricati`, `visura_terreni`,
  `visura_soggetto`) for the same comune/provincia/foglio/particella/subalterno and `tipo_catasto` (`F`/`T`; none = either);
* ids that differ only by a final `-F`/`-T` are the two guesses for an identifier of unknown type: when one is stored,
  both go;
* other rows (any other command) are never touched;
* a row with `reuse` = `force` is a repeat on purpose: it is never pruned (here and in `run-session`) and the runner sends it again
  whatever the service has cached (so no "download it again?" question).

A copy of the file goes to `backups/` next to it. `--dry-run` only reports.

## Generated files

`sister batch convert-pvp-csv` writes the PVP lists in this format (one table, `command` column). Reports that are not meant to be run have no table markers, and `build-list` refuses them with "nessuna riga di
query". `inputs/to_be_downloaded.batch.md` is the single job file for the parcels still to download (one table per source:
PVP OCR, `real_estates`, `pvp_enriched`); the report it replaced is archived in `inputs/_old/`.
