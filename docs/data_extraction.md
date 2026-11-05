# What each query returns and where it lands in the database

Two principles drive the extraction:

1. **Read every HTML page first, request documents second.** Everything up to the *Tipo di visura* form is plain
   HTML without CAPTCHA (see `docs/captcha_pages.md`), so a run reads all of it, stores it, and only then submits the
   document requests (CAPTCHA, billable). A CAPTCHA that nobody solves no longer costs the data of the immobili that
   come after it. Flowcharts: [`property_visura_workflow.svg`](property_visura_workflow.svg) (queries `search` /
   `intestati`) and [`person_search_workflow.svg`](person_search_workflow.svg) (persona fisica). Regenerate them with
   `dot -Tsvg <name>.dot -o <name>.svg` whenever a query changes.
2. **Keep what the pages say, split what they pack.** Portal cells combine several facts (`RA/103` = sezione + foglio,
   `Proprieta' per 1/2` = diritto + quota, `ROSSI MARIO a ROMA (RM) il 01/01/1970` = name + birth place + date). They
   are split by `sister/result_parsers.py` (pure functions, unit-tested) before they reach the tables.

## The two phases of `search` / `intestati` (`run_visura`)

| Phase | Pages | CAPTCHA | Code |
|---|---|---|---|
| Search | SceltaServizio → Immobile → form → (Conferma assenza subalterno) → *Elenco Immobili* | no | `run_visura` |
| **1 — HTML** | for every immobile of the list: select radio → *Intestati* → table *Elenco Intestati* → *Indietro* (the list stays valid) | no | `_read_immobili_intestati` |
| (store) | `save_response` projects the JSON into the tables (see below) | | `database._persist_projection` |
| **2 — documents** | *Visura Per Immobile* and *Visura per Soggetto* → *Tipo di visura* form → CAPTCHA → *Inoltra* | **yes** | `_request_visura_documents` |
| Download | Richieste → *Salva* → `.p7m`/PDF → XML → typed tables | no | `_download_richieste_documents` |

* `richiedi_documenti=false` (CLI `--richiedi-documenti false`, API `form_fields`) stops after phase 1: HTML only, no
  CAPTCHA, nothing requested or downloaded. Default `true` (unchanged behaviour).
* One *Visura per Soggetto* per immobile, as before, but never the same owner (codice fiscale) twice in a run: the
  document is identical and each request costs a CAPTCHA. The step is marked `duplicate` in `results[].documents`.
* When the CAPTCHA is not solved the run ends as `needs_human`; the HTML data is kept and `documents_pending` lists the
  requests still to make (`{"result_index": 3, "kind": "soggetto"}`). `results[].documents` records each request as
  `requested` / `pending` / `duplicate` / `unavailable` / `unconfirmed`.
  `requested` means the portal acknowledged the request (page "Attesa": "Richiesta inoltrata"); `unconfirmed` means it was
  submitted but not acknowledged (the form came back, or another page): `submit_problems` holds the reason and
  `documents_unconfirmed` lists them. The "Codice di Richiesta" on that page is the same for every request of an account
  and year (kept as `portal_codes`, informational); the request id is the `id_richiesta` of its row in Richieste.
* After a submitted request SISTER forgets the immobili list, so phase 2 re-submits the search once per request.
  Phase 1 never needs to (that was 2 re-searches per immobile when both phases were interleaved).

## What each form returns

Columns observed in the stored responses (452 responses, 2026-10-09). `*` = no table column yet (kept in the raw JSON).

| Query | Portal result | Columns / fields | Lands in |
|---|---|---|---|
| `search`, `intestati` — fabbricati | *Elenco Immobili* | Foglio (`RA/103`), Particella, Sub, Indirizzo, Zona cens, Categoria, Classe, Consistenza, Rendita, Partita, Altri Dati\*; radio `visImmSel` (catasto, comune code, sub) | `visura_properties` (+ `cadastral_locations`) |
| `search`, `intestati` — terreni | *Elenco Immobili* | Foglio, Particella, Qualità, Classe, **ha / are / ca**, **Reddito dominicale / agrario**, Partita, Porzioni\* | `visura_properties` (`area` in m², incomes) |
| `intestati` (phase 1) | *Elenco Intestati* | Nominativo o denominazione (name + birth place + date), Codice fiscale, Titolarità, Quota, Altri dati\*; radios: sesso, luogo e data di nascita, sede | `cadastral_subjects`, `geographic_places`, `ownership_rights`, `visura_owners` (linked by `result_id`) |
| `soggetto` / `soggetto-immobili` | *Elenco immobili* per provincia | Catasto, Titolarità (`Proprieta' per 1/2`), Ubicazione, Foglio, Particella, Sub, Classamento (`Zona 1 Cat.A/4`), Classe, Consistenza, Rendita, Partita; `provincia`, `visImmSel`; with `--con-intestati` the owners of each row | `visura_properties` + the queried subject as owner (`visura_owners`) |
| `azienda` / persona giuridica | *Elenco soggetti*, then *Elenco Province* and *Immobili* of **every** record | the properties of each record, tagged with `omonimo`, `omonimo_denominazione`, `omonimo_sede` | as `soggetto-immobili` |
| `elenco` | *Elenco immobili* (6 583 rows for one foglio in the fixture) | Foglio, Particella, Subalterno, Zona, Partita, Rendita, Indirizzo, categoria_gruppo | `visura_properties` |
| `indirizzo`, `partita` | list as `search` | + `indirizzo_trovato` | `visura_properties` |
| `nota`, `mappa`, `export-mappa`, `originali`, `fiduciali`, `ispezioni`, `ispezioni-cartacee`, `elaborato-planimetrico`, `riepilogo`, `richieste` | generic result table (`risultati`) | headers not yet verified on live data (most fixtures are empty or rejected by the form) | raw JSON only (`visura_responses.data`) |
| `visura-storica`, `soggetto-documento`, visura requests | document request (CAPTCHA) | → document, see next section | `visura_documents` and below |

`visura_responses.data` always keeps the complete JSON; the tables are a projection of it
(`sister db backfill projections` re-runs the projection over every stored response).

### Linking owners to properties

`visura_owners.result_id` and `visura_properties.result_id` point to the same `visura_results` row, so the
owner ↔ property views (`v_sister_owner_property_links` joins on `result_id` first) are exact. Before, both were always
NULL and a response with several properties joined every owner to every property. Where each owner list comes from:
`results[].intestati` (visura flow) → `immobili[].intestati` (owner → immobili with owners) → the queried subject itself
for a subject list that carries *Titolarità*. Top-level `intestati` of a response without per-property data belong to
its only property, or stay unlinked when there are several.

## Downloaded documents → typed tables

`_persist_flattened_xml` still stores every element in `document_xml_nodes`; `sister/xml_ingest.py` now also reads the
XML into the typed tables, which were empty:

| Document (`<Visura>` child) | Tables filled |
|---|---|
| `VisuraFabbricatiAttuale` / `Storica` | `building_current_states` (+ `building_identifiers`, `building_classifications`, `building_surfaces`, `related_parcels`), `building_addresses` (address history), `ownership_mutations` + `property_owners` (current owners as `mutation_index='current'`, former owners per mutation) |
| `VisuraTerreniAttuale` / `Storica` | `land_parcels` + `land_classifications` (one row per quality/class), `ownership_mutations` + `property_owners` |
| `VisuraSoggettoAttuale` | `document_subjects` (the queried person/company), `property_groups` → `building_units` (+ identifier, classification, surface) / `land_parcels`, group-level `ownership_mutations` + `property_owners` |
| all | `document_metadata` header: title, view type, service type, generation date/time, source, comune code, protocol/year, liquidation, requester |

Owners get the same normalisation as the HTML ones (CF → sex, birth place code; `FineDiritto` →
`start_date`/`end_date`). The history of classification changes in a *storica* document stays in
`document_xml_nodes` only (the typed tables hold one history record per document).
`visura_documents.response_id` is now set from the response's `downloaded_pdfs` (it was NULL for every document).

### Backfill of existing data

```bash
uv run sister db backfill            # = projections + xml
uv run sister db backfill xml --force --limit 20
```

`projections` rebuilds `visura_results/properties/owners` of every stored response from its JSON (idempotent, replaces
the rows of each response); `xml` fills the typed tables from the XML kept in `document_metadata.content` (documents cut
at 50 kB by the old limit are re-read from the file). Both are safe to re-run. Nothing runs automatically: they write to
the application schema.

## Not captured yet

(Schema note: the link columns of the typed XML tables exist since migration `20261010_xml_typed_columns`.)

* Catasto **comune code** (`H199`, in `visImmSel`; `CodiceComune` in the XML) has no column in `visura_properties` /
  `cadastral_locations`; it is only in the JSON and in the typed XML tables. Adding it needs an Alembic migration.
* *Altri Dati* / *Porzioni* columns of the lists, `RegimeConiugi` / `SoggettoComunione` of the diritti.
* Classification/identifier history of a *storica* visura as typed rows (kept in `document_xml_nodes`).
* The `risultati` tables of `nota`, `mappa`, `originali`, `fiduciali`, `ispezioni`, `elaborato-planimetrico`: capture a
  successful live result per query, add it to `tests/fixtures/single_step/`, then map its columns.


## Persona giuridica: one code, many registry records

For a codice fiscale / partita IVA SISTER lists one row per *registry record* that carries it (Denominazione, Sede, same
code): the same code can be stored under many names and seats (a region, its comuni and its demanio; ANAS has hundreds).
Each record is a different subject with its own properties, so `azienda` (`run_visura_persona_giuridica`) opens them one
by one (Ricerca → Elenco Province → Immobili) and **appends everything into one result for the code**:

* `immobili`: every property, with `omonimo` (1-based position), `omonimo_denominazione`, `omonimo_sede`;
* `omonimi`: one summary line per record (`immobili` count, `province`, `errore` when that record could not be read);
* `province`: the province table summed over the records; `omonimi_totale` / `omonimi_elaborati`.

A long list is not read past `SISTER_OMONIMI_MAX_SECONDS` (150 s; a client waits 300 s for an answer) or a lost SISTER session. The result is then
`incompleta: true` with `omonimo_prossimo` (the record to continue from) and `interrotta` (why). Options of the query:
`--omonimo-da N` (first record) and `--max-omonimi N` (at most N records per request).

`sister batch run-list` / `query batch --resume` follow this on their own: an `incompleta` answer is continued in the same
row with `omonimo_da` until the code is complete; the follow-ups are listed in the row's `request_ids`. If the session
ends in the middle the row is journaled `incomplete` with `omonimo_prossimo` and the next session resumes from that
record. A row that an older run journaled `completed` without `omonimi_totale` only has the list of records, so
`--resume` reads it again (forced), and `prune-list` / `answered_codes` do not count such an answer as done.

### More than 400 matches

SISTER lists at most 400 subjects for a search. Beyond that it answers on the search form itself with *"Superato il
limite massimo di 400 elementi. Aggiungere altri parametri per la ricerca"* and shows no result. The `azienda` and
`soggetto` flows recognise that page (`utils._too_many`) and answer with the clean error `TOO_MANY_ERROR` and
`troppi_risultati: true`, instead of reading the form as if it were a result list. It is a final answer, never retried.

* A **code** (codice fiscale / partita IVA) cannot be narrowed: the row ends as `too_many` (and `prune-list` counts it as answered).
* A **name** (`identificativo` is a denominazione) can: `sister batch run-list` / `query batch` split the row into one
  request per province of the seat (`provincia_sede`, the 107 options of the form, ids `<id>.<PR>`), each with its own journal
  line. The row is `completed` when every province was answered, otherwise `incomplete` (`split_pending`; `--resume` asks
  only those provinces). A province that still matches more than 400 is listed in `split_too_many`: narrow it with
  `comune_sede` or a more exact name. Names are never pruned as `too_many`, because the split is how they get answered.

## The intermediate pages of a subject search

A search by codice fiscale / partita IVA goes through three pages before the properties, and each now ends up in the result:

| Page | Shows | In the result (`data`) | Stored in |
|---|---|---|---|
| *Elenco Omonimi* (`RicercaPF.do` / `RicercaVisPNF.do`) | persons: cognome, nome, data e luogo di nascita, sesso, codice fiscale; companies: denominazione, sede, codice fiscale | `soggetti`: one entry per listed subject, selected or not (`tipo`, the decoded fields, `indice`, `selezionato`, and the whole row as `riga`) | `subject` (name and birth data, **only where missing**), `subject_record` (company records) |
| *Elenco Province* | provincia, fabbricati, terreni | `province` | `request_province` |
| *Elenco immobili* | the properties | `immobili` | `property`, `cadastral_unit`, owners |

Two pages show the subject: the *Elenco Omonimi* table (found by its radios; a person's table has no property-style header, so a header-based extractor skips it) and the "Soggetto selezionato" block of the *Elenco immobili* page (`result_parsers.parse_soggetto_selezionato`), which is read as well, so the details are captured even when the Omonimi page is not.

The Omonimi page used to be clicked through and its content dropped; it is the only place a code search shows who the subject is,
so a subject first met by code (name = code, `/persons` without a name) is named by the next search of that code. A name search
that lists several homonyms stores **all** of them, not only the one the flow opens. A company code with a single record names the
subject; with several records (ANAS-like) the records are stored and the subject keeps its code as name.
Parsers: `result_parsers.parse_omonimo_value` / `omonimo_details`; storage: `database._persist_listed_subjects`.
