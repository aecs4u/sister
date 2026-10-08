# SISTER batch for PDFs missing parseable companions

`inputs/pdf_parseable_inventory.csv` lists every PDF in the documents folder and
whether it has a same-name XML, JSON, GeoJSON, or signed P7M companion. The
current inventory has 92 PDFs: 12 already have a parseable companion, and 80 do
not. `inputs/pdf_parseable_batch.csv` groups those 80 source PDFs into 62
deduplicated SISTER queries.

Regenerate both CSV files after the documents folder changes:

```bash
python3 scripts/build_pdf_parseable_batch.py
```

Run the batch from the normal SISTER environment when the SISTER API is
available:

```bash
sister query batch \
  --input inputs/pdf_parseable_batch.csv \
  --command auto \
  --wait \
  --output-dir outputs/pdf_parseable_batch
```

SISTER writes one JSON result per deduplicated query. Each result includes
`source_files` and `scope_note`, so it can be traced back to the PDF filenames.
The batch uses SISTER's structured query responses; it does not download new
official XML/P7M documents. Those portal downloads require a human CAPTCHA.
Queries also return fresh results rather than recreating the historical date
and contents of an older PDF. The listing queries for `el_imm` and `el_sub` can
only be filtered by sheet through the current API, so their JSON may cover a
wider area than the source PDF's parcel.

To add a new batch command, keep its required fields and dispatcher mapping in
`sister/cli.py`'s `_BATCH_DISPATCHERS` in sync with the endpoint's client method.

## Running it (2026-10-06 run)

`sister` may not be installed as an executable; run the CLI as a module and give the service an API key when
`API_KEY` is set:

```bash
VISURA_API_KEY=<API_KEY> PYTHONPATH=. ../.venv/bin/python -m sister.cli query batch \
  --input inputs/pdf_parseable_batch.csv --command auto --wait --output-dir outputs/pdf_parseable_batch
```

Prerequisites: a logged-in SISTER session attached to the service (`docs/portal_session.md`) and **no edits to
`.py` files while it runs** (the service reloads and the row in flight is lost). The batch has no resume: rerun
only the failed rows from a CSV of those rows into a separate output folder, then copy the good results over.

The builder classifies each PDF's catasto from the *first* catasto named in its text, not from the mere presence
of "catasto terreni" (a Fabbricati visura also contains "Particelle corrispondenti al catasto terreni").

### What the run produced

* 62 queries; JSON results in `outputs/pdf_parseable_batch/` (`batch_NNNN_<command>.json`, failed originals kept in
  `_failed_before_rerun/`). Rows that need a CAPTCHA (property searches) wait for you to type it in the SISTER tab.
* Signed documents are **not** produced by the JSON queries. They come from the visura *requests* the property
  searches submit; download them with `POST /visura/download-documents` into `SISTER_FILES_BASE` using the naming
  convention of `docs/document_file_naming.md`.
* Still missing after that (compared by file name with the 80 PDFs): the historical property visure
  (`vi_sto_*`, request them with `sister query visura-storica`), the persona fisica subject visure (`vs_att_*`,
  `vs_sin_*`, request them with `sister query soggetto-documento`), and the company visure. The elenco /
  mappa / elaborato queries are JSON-only. `inputs/pdf_missing_documents.csv` lists the requests that were built.
* Elaborato planimetrico for FG103 P1714 / FG104 P2154 returns "nessun elaborato trovato"; `export-mappa` and
  `originali` need `--sezione` for comuni with sezioni (Ravenna); `nota` needs `--tipo-nota`.
