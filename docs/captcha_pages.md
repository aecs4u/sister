# SISTER CAPTCHA: which pages have one

The SISTER-native CAPTCHA is the field `input[name='inCaptchaChars']` ("Codice di sicurezza" image). It appears
on the **document-request form** (Tipo di visura → *Inoltra*), which is billable and delivers the official
XML/P7M. It was not seen on login, navigation, search or result pages.

Evidence: audit of the saved page HTML in `logs/pages` (1,421 pages, 2026-10-06). Counts describe that sample only.

## Pages that contain the CAPTCHA

| SISTER page | Reached from | Logged step | With CAPTCHA |
|---|---|---|---|
| `/Visure/vimm/InoltraRichiestaVis.do` | "Visura Per Immobile" | `visura_immobile_N` | 59 / 59 |
| `/Visure/vimm/TipoVisura.do` | "Visura per Soggetto" from an immobile's intestati | `visura_soggetto_N`, `visura_soggetto_unexpected_N` | 32 / 34 |
| `/Visure/vpf/TipoVisura.do` | Persona fisica → documento per soggetto | `form_visura_soggetto` | 3 / 3 |
| page after picking an owner (omonimo) | `SceltaIntestatiIMM.do` (still shows the form) | `omonimo_selezionato` | 1 / 3 |
| document form re-rendered after a wrong code | `Inoltra` rejected | `visura_inoltrata_N` | 1 / 57 |

A wrong code makes SISTER re-render the form with a **new** image, often at another URL, so a page change alone is
not proof that the code was accepted. `_wait_for_captcha` therefore loops until the field is gone.

## Pages with no CAPTCHA in the saved HTML

- Login and ADE/CIE pages (`login/*`, `sister_*` steps).
- Navigation: `scelta_servizio`, `provincia_applicata`, `immobile`, `persona_fisica`, `elaborato_planimetrico_form`.
- Search forms: every `form_compilato*` step (immobile, soggetto, persona giuridica, elenco, export mappa, elaborato).
- Result pages: `risultati_soggetto`, `risultati_pnf`, `risultati_elenco`, `risultati_export_mappa`,
  `risultati_elaborato_planimetrico`, and the intestati list.
- The Richieste list, and the page shown after a document was accepted (`visura_soggetto_inoltrata_N`).

## Limits of this evidence

- **Search and result snapshots prove less than they look.** `run_visura` and `_submit_and_extract` call
  `_wait_for_captcha` *before* logging the `ricerca` / `risultati_*` page, so a CAPTCHA that was shown and solved would
  not be in the saved HTML. The clean `form_compilato` pages and the project docs support "no CAPTCHA on search";
  the snapshots alone do not.
- **No saved pages exist for** ispezioni, ispezioni cartacee, mappa (EM), originali, fiduciali, nota, partita,
  indirizzo and ispezione ipotecaria. Whether they show a CAPTCHA is unknown.
- `outputs/*.json` holds `page_visits` for only one `soggetto` run (10 visits, no CAPTCHA); `outputs/pages` is
  screenshots only. The DB `page_visits` table was not audited.

## How the code handles it

- `_wait_for_captcha(page, timeout=120)` returns `False` when there is no CAPTCHA and `True` once a human solved it.
  It raises `CaptchaRequired` (`sister/models.py`) when nobody solved it in time or after 5 wrong codes.
  It never goes on as if the request had been submitted.
- `run_visura` catches `CaptchaRequired` inside the per-immobile loop, keeps the immobili/intestati already
  extracted and adds `"needs_human"` to the result.
- Any other caller lets it propagate; the response error then starts with `CaptchaRequired: CAPTCHA_REQUIRED`.
- Polling (`GET /visura/{id}`, the web form, `VisuraClient.wait_for_result`, the CLI) reports status `needs_human`
  and stops polling.
- Policy: the CAPTCHA is **not** automated. It protects official document requests under the SISTER agreement
  (see [improvement_proposals.md](improvement_proposals.md) §3.2).

## Which flows can hit it

Only flows that submit a document request: `run_visura` with intestati (per-immobile and per-owner document
requests), `run_soggetto_documento`, `run_visura_storica`. Plain searches, elenco, soggetto and persona giuridica
result pages, and the paid ispezione flows do not submit through this form.

**Caveat (2026-10-06):** `request_documents` (the opt-out described in improvement_proposals §3.1) is *not* in the
source tree, so `run_visura` submits the document requests whenever `extract_intestati` is on.
