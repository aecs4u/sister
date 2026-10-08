# Single-step queries: one definition, four front ends

Every single-step query (`search`, `intestati`, `soggetto`, `azienda`, `elenco`, `indirizzo`, `partita`,
`nota`, `mappa`, `export-mappa`, `originali`, `fiduciali`, `ispezioni`, `ispezioni-cartacee`,
`elaborato-planimetrico`, `riepilogo`, plus `richieste-sister`, `ipotecaria-stato/-elenchi`,
`visura-storica`, `soggetto-documento`, `soggetto-immobili`) is defined once, in
[`sister/query_forms.py`](../sister/query_forms.py):

* its **inputs**: one `FormField` per input element of the real SISTER form (`portal` = its `name`
  attribute) with the parameter name, kind (text, select, radio, checkbox, date), choices and help;
* how it is **submitted**: CLI command name, API path, client method, required parameters.

Everything else is generated from that table:

| Front end | How |
|---|---|
| `sister query <command>` | `_make_query_command` in `cli.py` builds the Typer command (one option per parameter, required ones from the spec) |
| `sister query batch` | CSV columns are the command's parameters; rows go through `VisuraClient.submit` |
| `/web/forms` | `generate_single_step_params()` in `form_config.py` builds the group parameters |
| `POST /visura/{search_type}` | the generic route reads the query string; valid types come from the spec |

CLI, batch and web all submit through **`VisuraClient.submit(command, params)`**: the dedicated arguments of
the command go to the matching client method, every other parameter is an input of the SISTER form and
travels as `form_fields`. In the browser, `_apply_form_fields` (`utils.py`) fills those inputs just before
the form is submitted.

## Mode-dependent inputs

Some inputs exist only in one mode of a radio (the provisional identifier of an immobile, the historical map
request, search by surname instead of codice fiscale, registro particolare/generale). The spec marks them with
`requires=(mode parameter, value)`; the filler fills the inputs that exist in every mode first, then switches the form
to the required mode and fills the rest. Asking for inputs of two different modes, or for an input the form does not
offer at that office level (e.g. "restrizione foglio" on the national office), fails at once with a clear message
instead of waiting for the portal. The live check (fill every input of every real form and read it back) found
this; it can be repeated whenever the portal changes.

## Adding a query or an input

1. Save the portal form as `tests/fixtures/portal_forms/<name>.html` (only the `<form>` of the query; no
   personal data).
2. Add its `FormField`s and metadata to `query_forms.py`.
3. Implement the browser function (`run_*` in `utils.py`) and register it in `browser.py`
   (`_GENERIC_DISPATCHERS`), calling `_fill_richiedente_motivo` (which applies the form fields) before
   submitting.

The CLI option, the web input, the API parameter and the batch column then exist automatically.

## What the tests guarantee (`tests/test_query_forms.py`)

* every input of each saved portal form has a parameter, and the spec names no input the form lacks
  (a form that changes on the portal makes this fail);
* every parameter is an option of the CLI command and reaches the client call;
* every parameter reaches the service request through the API;
* `/web/forms` takes exactly the CLI options and renders an input for each, and its submit goes through
  `VisuraClient.submit`.

Result fixtures of each query (anonymised real responses) are in `tests/fixtures/single_step/`.
