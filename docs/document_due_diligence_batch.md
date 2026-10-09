# Document-derived due-diligence batch

`scripts/define_document_workflow_batch.py` scans local SISTER documents and writes one JSONL row per deduplicated job to a private output file. It does not contact SISTER or submit portal requests. The manifest contains fiscal codes and cadastral coordinates, so keep it under the gitignored `outputs/` directory.

## Jobs in the manifest

| Job | Target | Workflow | Included scope |
| --- | --- | --- | --- |
| `property_due_diligence` | Cadastral type, province, municipality, sheet, parcel, subunit, and section | `due-diligence`, depth `full`, history enabled | Current searches, owners, historical and cross-property expansion, risk and timeline stages, plus available map/building detail stages |
| `person_portfolio` | One validated 16-character natural-person codice fiscale | `portfolio`, depth `full` | Subject lookup, property expansion, ownership history, timeline, and risk stages |

Property jobs deduplicate by all cadastral fields, including subunit and section. Person jobs deduplicate by codice fiscale. Each row records how many source documents referenced that target; it does not expose source filenames.

## Build the manifest

```bash
uv run python scripts/define_document_workflow_batch.py \
  --documents /path/to/sister/documents \
  --output outputs/document_workflow_batch.jsonl
```

The command prints aggregate coverage counts, including complete targets, partial property locations, and person-context documents without a valid codice fiscale. The output file is restricted to the current user where filesystem permissions allow. Rebuild it after adding or changing source documents.

## Launch order and fee gate

Submit one `workflow_payload` at a time through the authenticated workflow stream endpoint (`POST /web/api/workflow/stream`) and wait for its terminal event before submitting the next row. This keeps the shared browser session serialized and makes failures attributable to a single target. The manifest is a definition only; generating it does not launch the jobs.

The base payloads set `include_paid_steps=false` and `auto_confirm=false`. Each row lists the portal-paid steps that would complete the full scope in `paid_steps_deferred`. After reviewing the portal fees, an approved paid run must set both flags to `true`; the workflow engine requires both. Property jobs defer `ispezione_ipotecaria` and `portfolio_ipotecaria`; person jobs defer `portfolio_ipotecaria`. Do not enable these flags as a bulk default before reviewing the fees.

The payloads use the largest supported expansion limits: 100-way fanout, 50 owners, 100 properties per owner, 50 historical properties, and 500 total workflow steps. These are hard service limits, so an unusually large portfolio can still be capped and should be reviewed for incomplete expansion. A history `nota` lookup also needs a note number; when no note number is supplied, that step is skipped by the workflow.

## Multi-step execution behavior

Within a workflow run, identical free query submissions reuse the first response, including its request ID. Paid mortgage inspections and document downloads are excluded, and `force=true` bypasses this reuse. The workflow summary reports the number of reused queries. Owner expansion is capped by the lower of `max_fanout` and `max_owners`.

Property, subject, and request-page steps include captured `page_visits` alongside their structured results. The visit records include the visited page details and captured form fields or errors, and are stored through the normal response persistence path when database storage is enabled. This keeps the source-page context available with extracted data such as property rows.

The Richieste lookup returns only requests with a `Salva` link, along with their request metadata and ID. It omits the session-bound download URL. Downloading is a separate action; the `/visura/download-documents` endpoint is not launched automatically by each workflow.

## Coverage limits

Only targets recovered from parseable local XML, P7M, PDF, and ZIP content are included. Property jobs require a complete province, municipality, cadastral type, sheet, and parcel; partial coordinates are counted but cannot form a valid workflow request. Person jobs require a valid natural-person codice fiscale; names without a recoverable code are not submitted as portfolio targets. Scanned PDFs without a text layer and unsupported formats can therefore leave targets unresolved.
