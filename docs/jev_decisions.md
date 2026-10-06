# Jev decisions in SISTER

SISTER uses TypeSafe Jev (`jev-latest`) only to resolve ambiguous cadastral
location options. Workflow functions own the search goal and fixed field
values; Jev receives the live choices only when local matching finds a tie.

Implementation: [`sister/jev.py`](../sister/jev.py) builds and validates Jev
requests; [`sister/utils.py`](../sister/utils.py) ranks dropdown options and
routes unresolved choices.

## Decision flow

1. The workflow function supplies the requested location and fixed form
   options, such as cadastre type, sheet, parcel, search mode, and reason.
2. SISTER supplies the current dropdown options. The local matcher removes
   labels containing `SOPPRESS` before comparing them.
3. Exact matches and a single top-ranked option are resolved locally. No match
   returns `None`; Jev is not asked to invent an unavailable option.
4. If multiple options tie, Jev may choose only among those supplied options.
   The selected opaque candidate ID is mapped to the option value locally.

The local ranking is case-insensitive. It selects an exact value/label first;
otherwise it scores a label prefix as `len(request) / len(label)`, a value
prefix as `len(request) / len(value) × 0.9`, and a label substring as
`len(request) / len(label) × 0.6`. Options within `0.02` of the top score count
as tied. The current workflow sends at most 32 tied candidates to Jev.

Jev is disabled unless both `SISTER_JEV_ENABLED` and `TYPESAFE_API_KEY` are
configured. When Jev is disabled or unavailable, the workflow uses the local
top-ranked option in DOM order by default. That fallback is deterministic, but
it may still be ambiguous. Set `SISTER_AMBIGUOUS_OPTION_POLICY=operator` to stop
instead; see [Failure and confidence rules](#failure-and-confidence-rules).

## Fixed controls and dynamic choices by page

“Fixed” describes the workflow intent or value supplied by the function.
“Dynamic” describes the choices or results SISTER returns at runtime. A
location request can therefore have a fixed target and a dynamic option list.

| SISTER page/stage | Fixed by workflow function | Dynamic from SISTER | Jev decision |
| --- | --- | --- | --- |
| `SceltaServizio.do` | Requested province, or `NAZIONALE`; submit `Applica`. | Current province/office labels. | Resolve a tied province match only. |
| `Persona fisica` form | Cadastre type, search mode, requester, reason, and search values. Try CF first; use Cognome/Nome only after no exact CF match. | Validation messages and subject results. | None. These actions and the fallback condition are fixed workflow logic. |
| Subject homonym results | Select the sole row whose value contains the exact requested CF. | SISTER’s returned subject rows. | None. CF matching and selection are local. |
| `Immobile` / `Elenco immobili` forms | Cadastre type, requested cadastral identifiers, and submit actions. | Comune and section dropdown choices, loaded for the selected office. | Resolve a tied comune/section match only; province selection belongs to `SceltaServizio.do`. |
| National subject property list | Iterate over returned offices and municipalities with properties; skip suppressed records. | Office links, municipality rows, counts, and JSON responses. | None. The loop follows returned data. |

The fiscal code is sent to SISTER as required by the CF search and used locally
to verify the exact homonym. It is never included in a Jev request.

## Multiple decisions on one page

A page can contain more than one independent dynamic choice. The
`find_best_option_matches` helper resolves exact and uniquely best controls
locally, then sends the remaining ties as named choice questions in one Jev
request. The adapter accepts up to 12 independent questions. The normal workflow
matcher sends at most 32 tied options per question; the lower-level payload
builder accepts up to 255.

The batch returns answers only after every question has a valid response. If
any answer is missing or invalid, the ambiguous-option policy applies to the
whole batch: by default every tied control uses its local fallback. If any answer is an explicit low-confidence abstention, the batch
stops and requires operator selection; it does not return partial selections.

Some controls depend on earlier choices. For example, SISTER may populate a
comune list only after the province is selected. Those choices must be made in
sequence, after the page refreshes. Stable controls remain function options and
are not sent as Jev questions.

Current workflows mostly call `find_best_option_match` one control at a time,
because their location options are loaded on successive pages or depend on a
prior selection. The batch helper is covered by local tests; no current workflow
uses it for multiple controls yet.

## Jev instructions by control

Every request is limited to these controls:

| Control | Required decision |
| --- | --- |
| Province (`listacom`) | Select the requested cadastral province/office. For `NAZIONALE`, select only the nationwide option. Do not match another province by shared prefix or substring. |
| Municipality (`denomComune`, `comuneCat`) | Select the requested comune from the municipality options. Do not substitute a province, district, similarly named comune, or partial text match. |
| Section (`sezione`) | Select the requested cadastral section code or label. Do not confuse it with a municipality, urban section, or unrelated number. |

Shared rules require Jev to return one supplied candidate ID, compare complete
labels, use the DOM control label and fieldset legend to confirm the selector’s
meaning, and avoid inventing choices. An abbreviation or alternate spelling is
acceptable only when it identifies one clear option. When no option clearly
matches, Jev should report low confidence.

## Failure and confidence rules

The adapter accepts an answer only when:

- its type is `choice` and its candidate ID was included in that question;
- confidence and the selected candidate’s probability both meet
  `SISTER_JEV_MIN_CONFIDENCE` (default `0.95`); and
- the selected candidate’s probability exceeds every other candidate’s by at
  least `0.15`.

Non-finite or out-of-range confidence/probabilities make an answer invalid.
An absent probability map gives the selected candidate probability `0`, so the
answer abstains under the threshold/margin checks.

| Jev result | Workflow behavior |
| --- | --- |
| Accepted answer | Map the candidate ID back to the live option value and select it. |
| Low confidence or insufficient margin | Raise `JevAbstentionError`; stop before applying the ambiguous choice. |
| Missing/invalid answer, guard rejection, timeout, or provider error | Apply the ambiguous-option policy below. |
| Jev disabled or API key missing | Make no Jev request; apply the ambiguous-option policy below. |

`SISTER_AMBIGUOUS_OPTION_POLICY` decides what happens to a tie that has no
accepted Jev answer:

| Policy | Behavior |
| --- | --- |
| `local` (default) | Log a warning, select the local top-ranked option, and record it as `local_tie_break` in the extraction result. |
| `operator` | Raise `AmbiguousOptionError` before selecting anything, so an operator must choose. |

`JevAbstentionError` is a subclass of `AmbiguousOptionError`, so callers can
catch both stop conditions with one exception type. Exact and unique matches,
and ties that Jev resolves with an accepted answer, are never stopped by the
policy. In a page batch, the policy applies to the whole batch: either every
tied control falls back locally, or the batch stops with no partial selections.

## Reviewing tie resolutions

Every tie resolved during an extraction is recorded. The extraction result
carries an `ambiguous_options` list, added by `_run_with_network_json` in
[`sister/browser.py`](../sister/browser.py). The key is present only when at
least one tie occurred:

```json
"ambiguous_options": [
  {
    "control": "denomComune",
    "requested": "PALERMO",
    "candidates": ["PALERMO NORD", "PALERMO EAST"],
    "selected": "PALERMO NORD",
    "resolution": "local_tie_break"
  }
]
```

`resolution` is `jev` (with a `confidence` field) or `local_tie_break`. Review
`local_tie_break` entries before relying on the extracted data, because these
selections were not confirmed by Jev or an operator. Exact and unique matches
are not recorded. A stopped selection raises an error and produces no result.

## Data sent to Jev

The adapter builds the request from the live Playwright DOM. A request includes
the SISTER page path (without query parameters), the requested location label,
visible candidate labels, and short task instructions. Batch requests contain a
separate named question and candidate set for each decision. Candidate IDs such
as `option_0` map to SISTER option values locally.

Optional DOM context contains only the control role, accessible/control label,
row header, and enclosing fieldset legend. The requested location label is sent
because it defines the choice. Other entered form values, screenshots, raw HTML,
option values, hidden inputs, property rows, and captured network responses are
excluded. The adapter rejects requested/candidate labels matching a fiscal-code
or 11-digit VAT-number pattern. DOM context is also excluded for hidden controls,
labels over 120 characters, or labels containing a run of 7 or more digits.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SISTER_JEV_ENABLED` | unset (off) | `1`, `true`, `yes`, or `on` enables Jev; an API key is also required. |
| `TYPESAFE_API_KEY` | unset | Bearer token for `https://api.typesafe.ai/v1/systemone`. |
| `SISTER_JEV_MIN_CONFIDENCE` | `0.95` | Confidence and selected-probability threshold; clamped to `[0, 1]`. |
| `SISTER_JEV_TIMEOUT_SECONDS` | `4` | HTTP timeout per Jev request. |
| `SISTER_AMBIGUOUS_OPTION_POLICY` | `local` | `local` uses and records the top-ranked option for an unresolved tie; `operator` stops with `AmbiguousOptionError`. Other values mean `local`. |

On import, `sister.jev` loads the project `.env` and the `.env` two directories
above the project root. Variables already set in the process environment take
precedence.

## Network JSON capture

JSON capture is separate from Jev. [`sister/browser.py`](../sister/browser.py)
starts [`SISTERJsonCapture`](../sister/network_json.py) around an extraction.
It captures successful 2xx JSON, anti-XSSI-prefixed JSON, and JSONP responses
from SISTER/Agenzia delle Entrate hosts, up to 4 MiB per response, 16 MiB total,
and 100 responses. Keys containing password, token, CSRF, session, cookie,
authorization, captcha, or OTP are stripped recursively.

Property-shaped records with both sheet and parcel identifiers can supplement
DOM-extracted rows when the same property identity is not already present.
JSON rows with any value containing `SOPPRESS` are skipped. The captured JSON is never sent to Jev.

## Tests

Offline tests use HTML fixtures and mocked Jev transports; they make no Jev or
SISTER network calls and do not use screenshots:

```bash
python -m pytest tests/test_jev.py tests/test_jev_dom.py tests/test_network_json.py tests/test_browser_dispatch.py -q
```

The opt-in live test is `tests/test_jev_extraction_workflow.py`. It uses the
real SISTER service through the existing authenticated Playwright/CDP session.
It provides no credentials, leaves shared Chrome open, and downloads no
documents. Jev is enabled for the test, but an API request occurs only if the
local matcher finds a tie.

```bash
SISTER_LIVE_TEST=1 \
SISTER_LIVE_TEST_FISCAL_CODE='your-test-CF' \
SISTER_LIVE_TEST_SURNAME='La Vardera' \
SISTER_LIVE_TEST_GIVEN_NAME='Ismaele' \
python -m pytest -m live tests/test_jev_extraction_workflow.py -q
```

The live test searches by CF first and falls back to Cognome/Nome only when
SISTER does not return the exact subject. Set `SISTER_LIVE_TEST_PROVINCE` to
limit it to one office; otherwise the search is national. If SISTER presents a
CAPTCHA, an operator must solve it manually.
