# Single-step query fixtures

One folder per `sister query` command, one file per outcome seen on the live SISTER portal
(`success`, `no-match`, `form-rejected` = SISTER's own validation message, `empty` = completed with no
rows that could not be verified against real data). Each file holds the `request` parameters and the
`response` the service returned for them.

Captured on 2026-10-06 against the Ravenna test property (foglio 103, particella 1714, sub 2). Personal
data is anonymised: names are replaced by `ROSSI`, tax codes by `TSTUSR..`, dates of birth by
01/01/1970; the embedded XML documents are dropped and long result lists are cut to 3 rows
(`trimmed_lists` records the original size).

The matching portal forms are in `../portal_forms/`; `tests/test_single_step_fixtures.py` checks both.
