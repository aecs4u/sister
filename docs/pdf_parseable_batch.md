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
