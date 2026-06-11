# Retrieving a SISTER visura `.p7m` (Persona Fisica → Immobile)

Runbook for fetching a cadastral **visura** for a person (by Codice Fiscale) from the
SISTER portal (`sister3.agenziaentrate.gov.it`) and saving the signed document.

**Outcome:** a CAdES‑signed XML document — a `DOC_<idRichiesta>.p7m` file
(`DER Encoded PKCS#7 Signed Data`) whose payload is XML conforming to
`Visure_SIT_ver1.0.xsd`. **No JSON is transferred** — the portal is a server‑rendered
Struts app; the only "data file" is the signed XML inside the `.p7m`.

---

## 0. Prerequisites

- A **CDP‑shared Chromium** running and authenticated to SISTER (manual CIE/SPID login).
  - Launch Chrome with `--remote-debugging-port=9222 --user-data-dir=<persistent profile>`.
  - Point sister at it via `.env`: `BROWSER_CDP_ENDPOINT=http://localhost:9222`, then restart sister.
  - Drive the live page from a script using `playwright … connect_over_cdp("http://localhost:9222")`.
- A valid, authenticated SISTER session (breadcrumb shows *Home dei Servizi / Visure*, province
  dropdown `select[name='listacom']` has > 1 option).

> ⚠️ **Do NOT** issue concurrent requests to `SceltaServizio.do` from a second tab (e.g. a
> naive keep‑alive). SISTER serializes the session and will return `lock.html`
> ("Utente gia' in sessione"), breaking the in‑flight flow. To keep the session alive, rely on
> normal click activity or ping a *static* same‑origin asset — never a flow servlet.

---

## 1. Persona fisica search form

If not already on the form (`…/Visure/vpf/DataRichiesta.do`, title *Ricerca persona fisica*):

1. Go to `https://sister3.agenziaentrate.gov.it/Visure/SceltaServizio.do?tipo=/T/TM/VCVC_`
2. Select province: `select[name='listacom']` → choose **NAZIONALE** (nationwide) or a specific province.
3. Click `input[type='submit'][value='Applica']`.
4. Click the left‑menu link **"Persona fisica"**.

## 2. Search by Codice Fiscale  ⚠️ select the radio FIRST

On the persona‑fisica form the default search mode is **Cognome**. You MUST switch to the
Codice Fiscale radio before filling, or you get
*"Il campo Cognome è obbligatorio … Codice Fiscale non deve essere riempito"*.

1. Check the CF radio: `input[name='selDatiAna'][value='CF_PF']`  ← value is `CF_PF`, **not** `CF`.
2. Fill `input[name='cod_fisc_pf']` with the codice fiscale (uppercase).
3. (optional) `input[name='richiedente']`, `input[name='motivoText']`.
4. Click `input[type='submit'][value='Ricerca']` → page `…/vpf/RicercaPF.do` (title *Risposta*).

## 3. Elenco Omonimi → pick the person

- Table columns: Cognome · Nome · Data di nascita · Luogo di nascita · Sesso · Codice Fiscale.
- Check the first/desired `input[type='radio']` (value token e.g.
  `22702531#0#IACOVINO#FRANCESCO#CVNFNC98D12D423G#ERICE#12/04/1998#TP`).
- Click `input[type='submit'][value='Ricerca']` → `…/vpf/SceltaOmonimiPF.do` (title *Elenco Province*).

## 4. Elenco Province → pick province, list immobili

- Table: Provincia · Fabbricati · Terreni · Comuni.
- Check the province `input[type='radio']` (e.g. value `AG#AGRIGENTO`).
- Click `input[type='submit'][value='Immobili']` → `…/vpf/SceltaVisuraNazionalePF.do`
  (title *Elenco immobili del soggetto*).

## 5. Elenco immobili → choose the immobile

- Table: Catasto · Titolarità · Ubicazione · Foglio · Particella · Sub · Classamento · …
- Check the immobile `input[type='radio']`.
- Click `input[type='submit'][value='Visura Per Immobile']`
  → `…/vpf/SceltaVisuraImmSoggPF.do` (title *Tipo di visura*).

## 6. Tipo di visura — options + CAPTCHA

Form fields on `SceltaVisuraImmSoggPF.do`:

| Field | Selector | Notes |
|---|---|---|
| Intestati | `input[name='intestati']` | `1` = Con intestati, `0` = Senza |
| Tipo visura | `input[name='tipoVisura']` | `0` = Completa, `3` = Storica analitica, `4` = sintetica |
| Situazione al | `input[name='giorno'] / mese / anno` | default = today (maxlength 2 on gg/mm!) |
| Formato documento | `input[name='tipoDocFornitura']` | values `PDF` / `XML` |
| Oggetto | `input[name='oggetto']` | free text, pre‑filled |
| **CAPTCHA** | `input[name='inCaptchaChars']` | **human step** — read the image / use audio |

1. Set format: check `input[name='tipoDocFornitura'][value='XML']` (or `PDF`).
2. **CAPTCHA — human‑in‑the‑loop (the automation must PAUSE here):**
   - If the page shows a *Codice di sicurezza* image (`img[src*='captcha']`) and the field
     `input[name='inCaptchaChars']` is present, **wait for the user** to read the image and
     supply the code. Do **not** guess/auto‑submit — a wrong code is rejected and the image
     regenerates (so any previously read value is stale).
   - Fill `input[name='inCaptchaChars']` with the user‑provided code.
3. Click `input[type='submit'][value='Inoltra']`.
4. **Verify acceptance / retry loop:**
   - **Accepted** → the page advances to *Documento pronto* (`CheckRichiesta`). Continue to step 7.
   - **Rejected / re‑rendered** → the form comes back (e.g. `…/TipoVisura.do`) still showing
     `input[name='inCaptchaChars']` and a **new** captcha image. Go back to step 2 (wait for the
     user again with the *new* image) and re‑submit.

> The CAPTCHA must be solved by a human and expires quickly. The workflow **blocks** on user
> input at this point — fill it immediately before Inoltra. (See the orange "WAIT for user input"
> node in `person_search_workflow.svg`.)

## 7. Documento pronto → download the `.p7m`

After a correct CAPTCHA you land on `…/Visure/CheckRichiesta?par=VC&idRichiesta=<id>&…`
(title *Documento pronto*): *"IL DOCUMENTO RICHIESTO E' PRONTO … resterà disponibile per una
settimana nella sezione RICHIESTE."* Buttons: **Apri · Salva · Indietro**.

- Click **Salva** → triggers a browser **download**.
  - Source URL: `https://sister3.agenziaentrate.gov.it/Visure/ConsultazioneRichieste.do`
  - File: `DOC_<idRichiesta>.p7m` (e.g. `DOC_2008928868.p7m`), `DER Encoded PKCS#7 Signed Data`.
- In Playwright, capture it with `page.expect_download()` and `download.save_as(path)`.
- The same document stays for ~1 week under the left‑menu **"Richieste"** section.

## 8. Extract the XML from the `.p7m`

```bash
openssl cms -verify -noverify -inform DER -in DOC_<id>.p7m -out visura_<id>.xml
# fallback: openssl smime -verify -noverify -inform DER -in DOC_<id>.p7m -out visura_<id>.xml
```

`-noverify` skips signer‑certificate chain validation (we only want the payload). The result is
XML (`<Visura …><VisuraFabbricatiAttuale>…`) with: `TitoloVisura`, `DatiRichiesta`,
`ImmobileFabbricati` (identificativi, indirizzo, classamento, superficie) and `Intestazione`
(each `Intestato`: `Nominativo`, `CF`, `DirittiReali` quota/diritto).

## 9. Navigating back (Indietro)

Each results page has an `input[type='submit'][value='Indietro']` that steps one level back up
the flow (the URLs change to `Indietro*` servlets but the titles match the forward pages).
Verified chain (each click = one level back):

```
Documento pronto (CheckRichiesta)
  └─Indietro→ Elenco immobili del soggetto   (/Visure/vpf/IndietroVisImmSogg.do)
       └─Indietro→ Elenco Province           (/Visure/IndietroListaProvince.do)
            └─Indietro→ Omonimi / "Risposta" (/Visure/vpf/IndietroOmonimiPF.do)
                 └─Indietro→ Ricerca persona fisica (form)
```

So **two Indietro clicks** from *Elenco immobili* land on the *Omonimi* (Risposta) page; from
there go forward again (select omonimo → Ricerca → select provincia → Immobili) to return to the
immobili list.

## 10. Alternative: list all intestati of an immobile

Instead of generating a per‑immobile visura, you can list every owner of the selected immobile:

1. On **Elenco immobili del soggetto** (`/Visure/vpf/SceltaVisuraNazionalePF.do`), check the
   immobile `input[type='radio']`.
2. Click `input[type='submit'][value='Intestati']`
   → `/Visure/vpf/SceltaVisuraImmSoggPF.do` (title **Intestati**).
3. Table **Elenco Intestati**: Nominativo o denominazione · Codice fiscale · Titolarità · Quota.
   Radio value token: `<id>#<id2>#<NOMINATIVO>#<CF>#<sesso>#<luogo>#<data>`.
4. Buttons there: **Immobili** (back to the list), **Visura per Soggetto**, **Indietro**.

This view is read‑only navigation (no extra charge) and mirrors the `<Intestazione>` block of the
visura XML.

### Generating a per‑soggetto visura (`Visura per Soggetto`)

Selecting an intestato row and clicking **Visura per Soggetto** leads to the *Visura soggetto*
options page (`/Visure/vpf/SceltaIntestatiPF.do`, then `/Visure/vpf/TipoVisura.do`):

- Options: `intestati`, **Sintetica / Analitica** (`tipoVisura`), `tipoDocFornitura` (PDF/XML),
  `oggetto` (pre‑filled "Visura <NOMINATIVO>").
- Then the **same CAPTCHA human‑in‑the‑loop** as step 6: **wait for the user** to fill
  `input[name='inCaptchaChars']`, `[Inoltra]`, and on rejection re‑wait with the new image.
- On success → *Documento pronto* → **Salva** → `DOC_<idRichiesta>.p7m`
  (payload root `<VisuraSoggettoAttuale>`). Extract as in step 8.

> Per the iteration rule, do this for **each** intestato row whose visura isn't already obtained —
> but each `Inoltra` is **billable and CAPTCHA‑gated**, so generate them one at a time with the
> user supplying each security code.

---

## Network interception summary

Captured while clicking **Salva** (`playwright` response listener):

- 1 response, from `…/Visure/ConsultazioneRichieste.do`, delivered as the binary `.p7m` download.
- **0** `application/json` responses. There is **no JSON API** behind these pages.

## Page/URL map

| Step | URL | Title |
|---|---|---|
| Service chooser | `/Visure/SceltaServizio.do?tipo=/T/TM/VCVC_` | Scelta province |
| PF form | `/Visure/vpf/DataRichiesta.do` | Ricerca persona fisica |
| Omonimi | `/Visure/vpf/RicercaPF.do` | Risposta |
| Province | `/Visure/vpf/SceltaOmonimiPF.do` | Elenco Province |
| Immobili | `/Visure/vpf/SceltaVisuraNazionalePF.do` | Elenco immobili del soggetto |
| Tipo visura | `/Visure/vpf/SceltaVisuraImmSoggPF.do` | Tipo di visura |
| Documento pronto | `/Visure/CheckRichiesta?par=VC&idRichiesta=<id>` | Documento pronto |
| Download | `/Visure/ConsultazioneRichieste.do` | (binary `.p7m`) |
