# SISTER downloaded document filenames

Downloads from the SISTER **Richieste** page are stored with a stable basename
built from the parsed document data. The filename does not depend on the
free-form `Oggetto` text shown by the portal.

## Naming rules

Property visure use the view, province code, sheet and parcel; a subunit is
included when present:

```text
vi_{att|sto|sin}[_ter]_{PROV}_FG{foglio}_PT{particella}[_SUB{subalterno}]
```

Examples:

```text
vi_sto_PA_FG134_PT122_SUB21_2026-10-06.xml
vi_att_RA_FG104_PT2154_SUB68_2026-10-06.p7m
vi_sto_ter_RA_FG002_PT246_2026-10-06.xml
```

Subject visure use the view, the queried fiscal identifier, the office that answered and
the requested cadastre scope:

```text
vs_{att|sto|sin}_{codice_fiscale-or-partita_iva}[_{PROV}][_{comune code}]_{CE|CF|CT}[_al{YYYY-MM-DD}]
```

`PROV` is the queried province (the same person asked at Ravenna and at Palermo gives two
different documents), the comune code is added when the query was limited to one comune, and
`_al<date>` appears on a storica/sintetica visura whose "situazione al" date is not the
request date. The cadastre token is never omitted: `CE` means both cadastres (`TipoCatasto=E`,
also used when the document does not say), `CF` means buildings, and `CT` means land. For example:

```text
vs_sin_FRSLSE77B54E730C_RA_CE_al2022-06-04_2026-06-04.p7m
```

Every download includes the date of its SISTER request (ISO) after the descriptive
basename, such as `vs_sin_FRSLSE77B54E730C_CE_2026-10-06.p7m` (the date, never the time).
If another file already has that name, the repeated request also gets the practice
number and year after the date (`_2026-10-06_T353113_2026`). The reference date of the
visura (`SituazioneAl`) stands in when the request date is unknown; without either date
the name stays plain while it is free, then takes the practice number and year (or
`_DOC_{id}`).

A PDF and a P7M are separate requests and can carry different dates. Code that pairs
them uses `utils._document_key(stem)`, the stem without the ISO date, never the exact stem.

## Source filename links

The downloaded file is stored in `SISTER_FILES_BASE` (the folder the web UI reads) when it is set, otherwise in
`SISTER_OUTPUTS_DIR/documents`. A symlink with the original SISTER download name, usually `DOC_{id}.p7m`, is stored in
the `document_links` directory (`SISTER_DOCUMENT_LINKS_DIR`, default `<outputs>/document_links`) and points to the
downloaded file.

For a signed `.p7m`, the XML payload is read and parsed in memory; no extracted `.xml` sibling or extensionless copy is
written. A separately downloaded `.xml` remains its own document. This keeps a signed file from being indexed as
multiple documents by folder imports.

New Richieste downloads use this convention automatically. Existing files are not renamed on startup; use the
maintenance scripts below to review and migrate them. Their normal mode is a dry run. Inspect each plan before using
`--apply`, which changes files and database records. The filename revert command applies the restoration immediately.

## Existing document maintenance

Run commands from the repository root. Set `DOCS` to the document directory and `LINKS` to its source-link directory:

```bash
DOCS=/path/to/documents
LINKS=/path/to/document_links
```

Add the request date to existing filenames. The date comes from the database, then the document, then the file's
modification time. The script updates database paths, `.plan.json` sidecars, and source links; it writes a database
backup and a reversible plan when applying changes. Files ending in `.meta` are left alone:

```bash
uv run python scripts/rename_documents_with_date.py --documents "$DOCS" --links "$LINKS"
uv run python scripts/rename_documents_with_date.py --documents "$DOCS" --links "$LINKS" --apply
uv run python scripts/rename_documents_with_date.py --documents "$DOCS" --links "$LINKS" --revert /path/to/rename_plan.json
```

Re-derive `vi_*` and `vs_*` names from document content and identify same-day duplicates. Identical repeats are moved to
a quarantine directory, with their database rows and aliases removed or redirected. Different documents that derive
the same name are kept and reported with the content fields that distinguish them:

```bash
uv run python scripts/normalize_documents.py --documents "$DOCS" --links "$LINKS"
uv run python scripts/normalize_documents.py --documents "$DOCS" --links "$LINKS" --apply
```

Remove legacy `.xml` and extensionless files only when their bytes match the payload of a neighboring `.p7m`. The
script also removes matching aliases and duplicate database rows, and backs up the database before applying changes.
Use `--skip-db` for a files-only cleanup:

```bash
uv run python scripts/cleanup_extracted_xml.py --documents "$DOCS" --links "$LINKS"
uv run python scripts/cleanup_extracted_xml.py --documents "$DOCS" --links "$LINKS" --apply
```

After files have been deleted manually, reconcile database rows and source aliases whose paths no longer exist. A
missing database row is removed only when a surviving `.p7m` has the same XML content and does not carry extraction
results that the missing row has. A dangling alias is re-pointed only when an equivalent surviving document can be
verified; unmatched rows and aliases are reported and kept:

```bash
uv run python scripts/reconcile_missing_documents.py --documents "$DOCS" --links "$LINKS"
uv run python scripts/reconcile_missing_documents.py --documents "$DOCS" --links "$LINKS" --apply
```

The scripts use the configured Sister database for record updates. Backups are written beside the documents under
`backups/` by default. Keep those backups and the normalization quarantine until the migrated folder and document pages
have been checked.
