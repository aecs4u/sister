# Analytical batch for missing XML/P7M companions

Generated from the current `visura_documents` inventory.

## Files

- `inputs/pdf_missing_documents_analytic.csv` is the corrected SISTER batch input.
- `inputs/pdf_missing_documents_property_storica_analitica.csv` contains the four corrected property-document requests; it was submitted separately after the initial `search` commands returned parcel data only.
- `inputs/pdf_missing_documents_analytic_inventory.csv` audits all 141 PDFs that lacked a same-stem non-PDF format. It marks 13 as already covered by an XML/P7M with a `_CE` scope name and classifies the remaining 128.
- `inputs/pdf_missing_documents_already_requested.csv` lists five analytical subject requests already submitted by the previous batch. They cover 19 PDF records and should be downloaded from Richieste instead of being submitted again.

The corrected batch has 56 deduplicated document requests covering 81 PDFs: 40 `visura-storica` property requests and 16 subject-document requests. `visura-storica` selects **Storica Analitica** on the Visura per Immobile form. All subject requests use `vista=analitica`; source PDFs named `vs_sin_*` are also mapped to analytical requests. These are fresh documents and cannot recreate an older PDF's original protocol or reference date.

## Exclusions

- 10 `isp_*` mortgage inspection PDFs are paid and excluded. The remaining standard cadastral visure are free under the user's SISTER account convention.
- 14 map, plan, floor-plan, and listing PDFs have no current XML/P7M retrieval command in this batch flow.
- 4 company/VAT subject PDFs cannot use `soggetto-documento`, which accepts persona-fisica CFs.

The batch contains no mortgage-inspection command.

## Submit and download

The batch has been submitted. The first run hit a closed CDP transport after one completion. After reattaching the browser, the failed subject and historical requests were retried and the four parcel-only `search` rows were replaced with Storica Analitica property-document requests. Submission records are under `outputs/pdf_missing_documents_analytic/` and `outputs/pdf_missing_documents_analytic_retry/`.

The request for `vs_sto_SMLRRN65L66G273G_T296244_2026.pdf` is included in the analytical `soggetto-documento` request for `SMLRRN65L66G273G`.

The request queue may pause for a human CAPTCHA. After the requests are ready in SISTER Richieste, fetch the available documents, including the five requests listed separately above:

```bash
curl -sS -X POST http://localhost:8025/visura/download-documents \
  -H "X-API-Key: ${VISURA_API_KEY}"
```

The previous batch's input was also corrected to `analitica`. Its output JSON is retained as history and records that the earlier FRSLSE request was submitted as `sintetica`; the new batch submits its analytical version.
