# PVP sales -> SISTER "visura per immobile" batches

One file per province (`<SIGLA>.csv`, 107 files), run with:

    sister query batch --input inputs/pvp_sales/PA.csv --wait --output-dir outputs/pvp_sales/PA

Source: `pvp_enriched.modelview.modelview_registries` joined to asset, sale and
municipality/province (same join as `land-registry/scripts/refresh_cadastral_parcel_auction.py`).
Read-only; generated 2026-10-10. Nothing has been submitted to SISTER.

## Guarantees
- One row per (provincia, comune, sezione, foglio, particella, subalterno, tipo_catasto); no key
  appears twice within or across files. `scope_note` lists the PVP sale ids that share the row.
- Every row passes sister's `validate_params("visura-storica", ...)`.

## Assumptions to review
- `cadastre_type_flag` is empty for every asset, so `tipo_catasto` comes from the registry
  `section` label (T/CT/NCT/TERRENI -> T; F/CF/NCEU/U/FABBRICATI -> F). With a subalterno it is F.
  With no hint, both a T and an F row are emitted (cost doubles for those parcels).
- Other `section` values are passed as `sezione`.
- Lists such as parcel `402-403-404` or sub `1-2-3` are split into separate rows, as the app's
  auction refresh does; a value like `1-12` is therefore read as two subs, not a range.
- `SU.csv` uses `provincia=SUD SARDEGNA`: PVP stores that province with the placeholder code `-`.
  Confirm the portal office name on one row before running the file.
- `_report/_unresolved.csv` lists registries that could not be turned into a query (4,788):
  unusable sheet, no municipality/province, or unusable parcel. Assets without a sale are skipped.
