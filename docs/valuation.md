# Property valuation (valore catastale and stima OMI)

How the subject-visura page (`/documents/<id>` for a `VisuraSoggetto*` document) values the properties it lists. For the
user-facing explanation see the `/valuation` page (`sister/templates/valuation.html`); its tables and worked examples are
generated from the code described here, so the two cannot disagree.

## Measures kept distinct

| | Valore catastale | Stima OMI indicativa | Fascia OMI personalizzata | Valutazione esterna inserita dall'utente |
|---|---|---|---|
| Meaning | IMU tax base from rendita / reddito dominicale | Official OMI range plus an indicative central estimate from its midpoint (or municipal zone medians when the exact zone is unknown) | User-transcribed estimate from a listing portal |
| Applies to | fabbricati and terreni | fabbricati of some categories only | buildings with an OMI range | any cadastral asset with a stable identifier |
| Inputs | the visura alone | visura + parcel position (map) + OMI database | user-entered minimum and maximum per building | provider, amount or range, optional date and URL |
| Nature | fiscal convention | order of magnitude, **not an appraisal** | personal adjustment; does not alter the published OMI band | unverified; may be an asking price, **not an appraisal** |
| Module | `sister/cadastral_values.py` | `sister/omi_quotes.py` | `sister/land_registry_kpis.py` + per-user PostgreSQL table | `sister/land_registry_kpis.py` + per-user PostgreSQL tables |

The cadastral and official OMI values are computed per table row and summed into the KPI cards by
`sister/land_registry_kpis.py` (`subject_land_registry_kpis`). Each OMI row keeps the official minimum/maximum range and
also calculates a central estimate, which OMI does not publish as a single amount, from the midpoint of that zone's
published price band. If the parcel's exact zone cannot
be resolved, the reported range spans the available municipality zones and its central estimate uses the median of the
published quote midpoints across those zones. When a parcel polygon is available, zone lookup uses a point inside that
polygon; otherwise it uses the stored cadastral centroid. This gives a representative figure without hiding the official
range or assigning an unknown parcel to a guessed zone. The asset cards remain whole-asset totals. When a user has
saved OMI adjustments, a separate personalized total substitutes those per-building ranges while retaining the original
OMI total. A separate owner assessment attributes building
market ranges by the subject's current right and declared share. Each signed-in user can add estimates from Immobiliare.it,
idealista, or another source, and select a preferred source per cadastral asset. The selected source feeds the separate
owner-right estimate where a full market value is available; the OMI range remains visible as its own official figure.
The cadastral share there is still a tax-base figure, not an economic valuation of the right.

## Valutazioni esterne inserite dall'utente

On a subject page, a signed-in user can add or update one estimate per provider and cadastral asset. The amount can be a
single figure or a minimum/maximum range; date, listing URL, and notes are optional. Entries and source preferences are
scoped to the user's stable auth identity and stored in `manual_property_valuation` and
`property_valuation_preference`. The database schema advances in Alembic revision
`20261106_manual_property_valuations`.

For buildings with an OMI range, the same panel allows a user to save a property-specific minimum and maximum in
`property_omi_override` (revision `20261109_property_omi_overrides`). A separate portfolio total substitutes those
values for the selected buildings and keeps the original OMI total and ranges visible. Leaving the maximum blank saves
a single amount. Clearing the adjustment restores the OMI values in the personalized total.

The preferred source is selected per asset. OMI is the default when it has an estimate; otherwise the first available
manual source is shown until the user chooses another. A selected manual estimate is used in the owner-right estimate
for that property, including the enfiteusi formula when an affrancazione amount is supplied or automatically estimated.
It does not replace or alter the official OMI column or the whole-portfolio OMI card. Manual estimates entered for land
are shown in the external-source panel, but land is not yet included in the owner-right market estimate.

SISTER stores the values entered by the user and does not fetch listing pages or verify their contents. Keep the source
provider, date, and URL with the amount: listing prices may be asking prices, stale, duplicated, or refer to different
surface/condition assumptions than the cadastral record.

## Proprietà per l'area (01T) e proprietà superficiaria (01S)

Una visura può indicare separatamente la proprietà per l'area (`01T`) e la proprietà superficiaria (`01S`). Sono diritti
distinti, non semplici quote percentuali della stessa proprietà. Il titolo costitutivo stabilisce come possono essere
usati, trasferiti e, se previsto, quando si estinguono. Gli articoli 952–953 del [Codice civile](https://www.normattiva.it/eli/stato/REGIO_DECRETO/1942/03/16/262/CONSOLIDATED/20260612)
disciplinano la proprietà separata della costruzione e gli effetti della scadenza del diritto di superficie. L'Agenzia
delle Entrate distingue i due diritti anche nei [codici della dichiarazione di successione](https://www1.agenziaentrate.gov.it/allegati_grandi/Specifiche_SUC13_20180307.pdf).

Non esiste una ripartizione universale del valore tra fabbricato e area. Come schema economico, i valori dei diritti
possono essere ricondotti al valore della piena proprietà solo quando la stima considera la stessa data, gli stessi
presupposti e tutti gli oneri rilevanti. Non è una formula automatica prevista per ogni caso.

### Valutazione commerciale

Per una compravendita occorre leggere il titolo e, se presente, la convenzione urbanistica o PEEP. Sono rilevanti almeno:

- la durata del diritto e la data di scadenza;
- eventuali limiti al prezzo o alle condizioni di trasferimento;
- canoni, obblighi e modalità di rinnovo, riscatto o trasformazione;
- gli effetti della scadenza, inclusa l'eventuale devoluzione della costruzione al proprietario del suolo.

Una convenzione comunale può vincolare il prezzo fino al riscatto, ma non è corretto sottrarre sempre un costo di riscatto
da un valore OMI: importo e condizioni dipendono dalla convenzione e dalle regole applicabili. Per un diritto a termine,
la durata residua e gli effetti della scadenza incidono sulla stima; una formula finanziaria come
`valore terminale / (1 + tasso)^anni residui` è soltanto un modello semplificato, che richiede ipotesi esplicite sul
valore terminale e sul tasso. La proprietà per l'area non equivale automaticamente al valore attuale del terreno libero.

Anche quando il diritto non ha una scadenza indicata, non si può presumere che la proprietà superficiaria valga sempre
come la piena proprietà o che il valore dell'area sia nullo: fanno fede il titolo, gli eventuali canoni e gli altri vincoli.
Una stima di mercato richiede quindi dati contrattuali e comparabili adeguati; le quotazioni OMI da sole non valorizzano
la ripartizione dei diritti.

Per impostare una stima specifica bisogna chiarire se serve un valore di mercato (acquisto/vendita) o un valore fiscale,
se il diritto è a termine o senza scadenza e quali limiti o costi risultano dal titolo e dalla convenzione.

### Enfiteusi (dominio utile)

For the subject-level assessment, SISTER estimates the emphyteuta's right only when both the property's selected full-value
range (OMI or a manually entered external source) and an affrancazione amount (supplied or estimated) are available:

```text
right range = [max(0, selected-source minimum - affrancazione amount for the whole asset),
               max(0, selected-source maximum - affrancazione amount for the whole asset)] × declared share
```

The optional whole-asset amount is read from `DirittiReali.ImportoAffrancazione` (or the normalized
`DirittiReali.affrancation_value`). Since normal SISTER visure do not supply it, the page offers an editable initial
estimate of `15 × annual cadastral income`: `reddito dominicale` for land and `rendita catastale` for buildings are used
only as proxies for the contractual annual canon. The proxy is not the legal amount due and may differ materially under
the title, the date and the applicable special regime. Each user's correction is saved in
`property_affrancation_amount` (Alembic revision `20261108_property_affrancation_amounts`); clearing the correction
restores the automatic estimate. OMI has no land quotes;
although the user can record an external estimate for a land parcel, the current owner-right calculation does not use
those land entries. No arrears or transaction costs are deducted.

Keep this indicative market allocation separate from the tax calculation. For registration-tax purposes, article 47
of the TUR defines the enfiteuta's value as full-property value less the amount due for affrancazione; for certain
taxable transfers or creations the base is 20 times the annual canon or, if greater, the value of the right. The amount
due depends on the title and applicable special regime; the page's 15-times proxy is a visible starting estimate, not a
universal legal calculation.
See the [Agenzia delle Entrate guide to article 47](https://def.finanze.it/DocTribFrontend/getContent.do?id=%7B9FA2A30A-0208-4787-B71A-91F9A25DE714%7D)
and [Law 607/1966](https://www.normattiva.it/atto/caricaDettaglioAtto?atto.codiceRedazionale=066U0607&atto.dataPubblicazioneGazzetta=1966-08-06&bloccoAggiornamentoBreadCrumb=true&classica=true&tipoDettaglio=vigente).

### Valutazione fiscale

Le regole dipendono dal tributo e dall'atto: successione, donazione, imposta di registro e base IMU non sono intercambiabili.
Per le successioni, l'articolo 14 del testo unico (come modificato dal [D.Lgs. 139/2024](https://www.normattiva.it/atto/caricaDettaglioAtto?atto.codiceRedazionale=24G00157&atto.dataPubblicazioneGazzetta=2024-10-02&bloccoAggiornamentoBreadCrumb=true&classica=true&tipoDettaglio=vigente))
prevede criteri distinti per piena proprietà, proprietà gravata da diritti reali di godimento e diritti di usufrutto, uso e
abitazione. Per gli acquisti abitativi ammessi al sistema del prezzo-valore, la rendita catastale rivalutata può usare il
coefficiente 110 per la prima casa (equivalente a `1,05 x 110 = 115,5` applicato alla rendita non rivalutata); la regola
ha specifiche condizioni e non è un criterio generale per successioni o donazioni. Si veda la [guida dell'Agenzia delle
Entrate sull'acquisto della casa](https://www1.agenziaentrate.gov.it/web_app_entrate/guida_acquisto_casa.html).

Di conseguenza, formule basate solo sugli anni residui e su un tasso non vanno presentate come metodo fiscale universale.
Per un calcolo fiscale occorre identificare il diritto esatto risultante dal titolo e applicare la disciplina vigente per
quel tributo e per quell'atto.

### Limiti attuali di SISTER

The asset KPI cards still sum whole-asset cadastral values and official OMI ranges. The separate owner assessment applies
declared shares to the cadastral base and allocates building estimates from the per-asset selected source by current full
ownership, usufruct, bare ownership, and enfiteusi. It does not estimate `01T`/`01S` or other rights. The visura alone does
not contain all the contractual terms needed for rights valuation; in particular it normally lacks the enfiteusi annual
canon and final affrancazione amount, so the automatic cadastral-income proxy must be reviewed against the title.

## Valore catastale (`cadastral_values.py`)

Pure functions, no I/O. Basis: art. 13, commi 4-5, D.L. 201/2011 (conv. L. 214/2011).

- Buildings: `rendita x 1,05 x moltiplicatore`. Multipliers by category: 160 (group A except A/10, C/2, C/6, C/7),
  140 (group B, C/3, C/4, C/5), 80 (A/10, D/5), 65 (group D except D/5; 60 before 2013), 55 (C/1). Groups E and F and
  unknown categories have no multiplier and are not valued.
- Land: `reddito dominicale x 1,25 x 135`. The reduced multiplier 75 (coltivatori diretti / IAP) is `LAND_MULTIPLIER_FARMER`;
  the visura does not carry that status, so it is **never applied** and the page says so.
- Amounts are `Decimal`, rounded half-up to cents; the page shows whole euros.
- `normalize_category` reads the code that *leads* the visura's `Categoria`, which is `"A/2: ABITAZIONI DI TIPO CIVILE"`
  (code plus description), not a bare code. Bare codes (`A02`, `C/6`) work too. This is a real-data requirement: matching
  the whole string finds nothing.

`BUILDING_MULTIPLIER_TABLE` is the human-readable copy of `building_multiplier`; `tests/test_valuation_docs.py` checks
every listed example against the function. Change a multiplier in both places and the test tells you if you forget one.

## Stima OMI (`omi_quotes.py`)

Source: the local PostgreSQL database **`omi`**, schema `public` (official Agenzia delle Entrate quotes, semiannual):

- `omi_ref_zone`: one row per zone and semester (`anno`, `semestre`, `zona`, `cod_comune_amm`, `zona_key`, ...).
- `omi_fact_quotazioni_valori`: one row per zone, typology and state (`cod_tipologia`, `tipologia`, `stato_conservazione`,
  `stato_prevalente`, `prezzo_min`, `prezzo_max` in EUR/m2, `superficie_nl_compravendita` L = lorda / N = netta).

Gotchas learned on the real data:

- Match the visura's comune code with **`cod_comune_amm`** (`G273` for Palermo). `cod_comune_catastale` is OMI's own code
  (`U1AA`) and does not match.
- Zone rows can be duplicated (section `''` vs `' '`), hence `SELECT DISTINCT`.
- `idx_omi_ref_zone_comune_period_zone`, `idx_omi_ref_zone_comune_catastale`, and `idx_omi_ref_zone_comune_istat`
  support lookup by each accepted comune key. Without the alternate-key indexes, PostgreSQL can choose a full scan for
  the overview's `OR` filter. The quote SQL selects the newest semester before joining the fact table, avoiding the full
  historical join. Keep these indexes on the `omi` database; `scripts/omi_quote_index.sql` installs them concurrently.
  Results are cached for `OMI_CACHE_DAYS` (30 days, 512 entries, memory only) and lookups run concurrently, bounded.
- **OMI has no land typologies**: the latest semester contains only building types (codes 1, 5-10 and 13-22).
  `zornade.zornade_zone_omi` in `aecs4u-stats` has "Seminativo/Uliveto/Vigneto" rows, but they are not land prices (Barolo's
  "Vigneto" is its residential quote), so terreni get no OMI estimate. Do not use those rows.

Connection: `OMI_POSTGRES_DSN`, else the database of `STATS_POSTGRES_DSN` / `CADASTRAL_POSTGRES_DSN` replaced by `omi`
(`omi_dsn()`); asyncpg, one short-lived connection per lookup. Failure handling: per-call timeout 8 s, connect 3 s,
`omi_zone_quotes` returns `None` (not `[]`) when the database cannot be queried and backs off 60 s for that comune/zone
only, so one slow lookup does not suppress unrelated quotes. A failure row says "Database OMI non disponibile"
instead of pretending the zone has no quote. There is no fallback to the older
`stats_service` quote path.

Pipeline per building unit (`subject_land_registry_kpis`):

1. The parcel must be on the local cadastral map (`map_lookup["parcel_markers"]`); its coordinates give the OMI zone
   through `stats_service.aget_omi_zone_at_point`.
2. `omi_zone_quotes(comune, zona)` returns the quotes of the zone's newest semester (`periodo`, e.g. `2025S1`).
3. `select_quote(quotes, categoria)` maps the category to typology codes (`OMI_TYPOLOGY_BY_CATEGORY`) and picks one
   quote: the one flagged prevalent (`stato_prevalente == "P"`), else the single `NORMALE`, else nothing. Ambiguity is
   reported, never guessed.
4. Estimate = `area in m² x prezzo_min` ... `x prezzo_max`. The area source is, in order: cadastral surface; explicitly
   recorded consistency in m²; or, for categories A with consistency in vani, `vani x (national mean cadastral surface
   per unit in that category / national mean vani per unit in that category)`. The last source is a coarse statistical
   proxy derived from Agenzia delle Entrate, Statistiche Catastali 2012, tables 12 and 13. It is shown in each row's
   formula note and does not enter the owner's cadastral area share. The `Superficie fabbricati` KPI fills missing
   surfaces from available cadastral consistency data, including this proxy, marks the combined total as approximate,
   and states how many unit surfaces are measured versus estimated. The note tells the reader to update the estimate
   with the actual values in the visura. Per-unit
   value ranges are summed for the card.

For the national fallback, the code stores the 2012 table means as `(mean surface m², mean rooms)` pairs by category and
uses their ratio as `m²/vano`. The factor is category-specific (for example, A/2 is `126 / 5.9 = 21.36 m²/vano` and
A/3 is `110 / 5.3 = 20.75 m²/vano`). See Agenzia delle Entrate,
[Statistiche Catastali 2012](https://www1.agenziaentrate.gov.it/mt/osservatorio/Tabelle%20statistiche/archivio/StatisticheCatastali2012_29102013.pdf),
tables 12 and 13 (A/10 uses table 22). The Agenzia's historical estimation method used local category-level averages where sample coverage
allowed; SISTER does not have those local factors, so its national proxy is more approximate. The estimated area remains
an estimated input, not a measured cadastral surface.

Category to typology (`OMI_TYPOLOGY_BY_CATEGORY`), following the Agenzia delle Entrate's correspondence
([Manuale OMI 2025, allegati, Tabella 1](https://www.agenziaentrate.gov.it/portale/documents/20143/265514/Manuale_OMI_ALLEGATI_2025_03_20.pdf/e5d8121b-0cb5-2e80-32db-fe6ddf3186ff?t=1742814287718)):

| Cadastral category | OMI typology (code) |
|---|---|
| A/1 | Abitazioni signorili (19) |
| A/8 | Abitazioni signorili (19), else Ville e villini (1) |
| A/2, A/3 | Abitazioni civili (20) |
| A/4, A/5, A/6 | Abitazioni di tipo economico (21) |
| A/7 | Ville e villini (1) |
| A/10 | Uffici (6) |
| A/11 | Abitazioni tipiche dei luoghi (22) |
| C/1 | Negozi (5) |
| C/2 | Magazzini (9) |
| C/3 | Laboratori (10) |
| C/6 | Box (13), else Autorimesse (16) |
| C/7 | Posti auto coperti (14), else scoperti (15) |

Two choices are ours, not the Agency's: A/8 falls back to *Ville e villini* because *signorili* is quoted in only ~65
comuni, and C/7 (tettoie) is valued as a parking space. The Agency also treats C/2 up to 30 m² and C/6-C/7 up to 50 m² as
*pertinenze* (a share of the house value); SISTER does not apply those caps. Before 2026-10-23 A/3 was mapped to
*economico* and A/4-A/6 were unvalued; economico quotes are about 20% below civili (median over 28,552 zones). Everything
else (A/9, B, C/4-C/5, D, E, F) is left unvalued with a reason (`OMI_UNVALUED_CATEGORIES` documents why). The non-residential
typologies OMI quotes but SISTER does not use yet (capannoni tipici/industriali for D/1 and D/7, centri commerciali for D/8,
uffici strutturati) have no official category correspondence; the Manuale only notes that D/8 covers both shopping centres
and structured offices.

## How the numbers reach the page

`web._render_doc_from_db` builds `land_registry_kpis` for soggetto documents and passes it to
`catasto/entity.html`, which:

- renders the KPI cards ("Valore OMI fabbricati", "Valore catastale (base IMU)") with a link to `/valuation`;
- embeds `table_estimates` as JSON (`data-land-map-omi-estimates`) and appends the columns client-side to the theme's
  tables (this page does not own the `buildings_table` / `lands_table` macros in `aecs4u-theme`).
- loads per-user external estimates and preferred sources for the listed cadastral identities, then shows the input and
  source-selection panel. Preferences also select the value range used by the owner-right assessment; the OMI table
  remains unchanged.

### Persisted calculation results

The full subject KPI result (cadastral values, OMI ranges, per-row estimates, ownership-right allocations and summary
tiles) is stored in PostgreSQL in `calculation_snapshot`. A snapshot is scoped to its source document or saved subject
request and viewer, and records a fingerprint of the parsed visura plus map inputs, the calculation version and the
calculation time. The page reuses a matching result for up to 12 hours; changed visura/map inputs or a new calculation
version force a refresh. The time limit also refreshes OMI-derived figures as the quote source changes. Updating a
user's manual valuation or preferred source invalidates that user's snapshots so the next page view recomputes the
ownership allocation with the new choice. The latest snapshot is retained per scope/viewer/calculation.

**Row alignment contract.** `table_estimates[group]["buildings"]` has one entry per `ImmobileFabbricatiS`;
`["lands"]` has one entry per `ClassamentoT`, plus one empty entry for a terreno without any (the theme draws an empty
row there). The script matches entries to rows by position and **skips a table whose row count differs**, so a mismatch
shows no column rather than values on the wrong row. Tables are identified by header count (buildings 11, lands 9)
*once, before any column is added* (`data-land-map-kind`); the ownership-history column uses the same tag. Do not
identify tables by header count afterwards.

A column appears only if at least one of its rows has a value.

## OMI zone overview (`/omi/valuation?comune=B696&zona=D4`)

`sister/omi_zone_overview.py` builds the page of one OMI zone from two local PostgreSQL databases, read directly with
short-lived asyncpg connections. OMI publishes once per semester, so the raw rows are cached for **30 days**
(`OMI_CACHE_DAYS`) per comune/zone, in memory and in `outputs/cache/omi_overview/<comune>_<zona>.json`
(`SISTER_OMI_CACHE_DIR` overrides the folder), so the cache survives the service's `--reload` restarts. Derived figures are
never cached, only the raw payload (OMI rows, NTN, stats context); bump `CACHE_FORMAT` if its layout changes. If the
database cannot be reached the stored copy is served even when expired (the page says so); a payload that is missing the
stats context is reused for only 5 minutes; an unknown comune is not stored; `&refresh=true` re-reads the databases.
Back-off after a database failure is 60 s. The older per-category lookup in `omi_quotes.py` uses the same TTL (memory only):

- **`omi`** (`OMI_POSTGRES_DSN`, see above): every published semester of the zone (`omi_ref_zone` x
  `omi_fact_quotazioni_valori`, 2004 onwards), including the rent columns `locazione_min/max` (text with decimal comma,
  parsed by `parse_number`), plus the comune's newest release and the zone's own last release for the comparison with the
  other zones; comune NTN from `omi_ref_comuni` / `omi_fact_compravendite_*` (empty for small markets: Capri has no
  residential NTN after 2018).
- **`aecs4u-stats`** (`STATS_POSTGRES_DSN`): `istat.comune_population_by_year`, `mef_irpef.income_average_comune` /
  `income_average_italia`, `serving.istat_house_price_index` (Italy, all dwellings: measure `4` = index 2015=100,
  `8` = annual change; there is **no Campania series**), and Eurostat NUTS3 population / GDP per head / births, deaths,
  net migration (the NUTS3 code comes from `istat.comune_nuts_by_year`). Eurostat household income, employment,
  tourism and business demography have no rows for the Capri NUTS3, so they are not used.

Rules the builders follow (all in pure functions, tested without a database):

- Keys: the comune matches `cod_comune_amm`, `cod_comune_catastale` or the numeric ISTAT code; the zone code repeats across
  comuni. The six-digit ISTAT code for the stats tables is the tail of OMI's `cod_comune_istat` (`15063014` -> `063014`).
- Zone rows are duplicated by `sezione`/`provincia`; `_normalise_rows` keeps one per (period, zone, typology, state).
- A typology's trend follows **one conservation state**: the one flagged prevalent in most periods (NORMALE on a tie).
  Following the per-semester flag would splice different states into one line.
- Variations (vs semester / 1 year / 5 years / since the first quote, compound annual rate, peak) use the midpoint of
  min-max and are looked up by semester, never interpolated across gaps. Gross yield = monthly rent x 12 / price.
- **Zones are redrawn between releases.** A zone missing from the comune's newest release is shown as "non più pubblicata"
  with the last data it has; `find_successor` suggests the zone that most likely replaced it (description overlap, or the
  same quote plus some overlap; Capri D4 -> D5 keeps its description and 6.500-10.000 range). It is a hint to open: the two
  histories are not merged.
- The cross-source chart rebases zone price (calendar-year mean), ISTAT HPI and IRPEF income to 100 on the first year they
  all have; population is added only when it covers that year. The affordability table is illustrative (years of average
  taxable income for 70 m2) and says so.
- Context is optional: when `aecs4u-stats` cannot be read the page shows the OMI part and a notice. When `omi` cannot be
  read the route answers 503.

The page is **not** a SISTER template: it lives in `aecs4u-theme` (`catasto/omi_valuation.html`, which extends
`base_template` so SISTER's `_base.html` supplies header and sidebar) and is built from the chrome-free component
`components/omi_zone_overview.html` (macros `omi_zone_search_form`, `omi_zone_result`, `omi_zone_list`,
`omi_zone_overview`, `omi_zone_overview_assets`, `omi_zone_overview_scripts`). Its CSS and the Chart.js drawing script
(`css/components/omi_zone_overview.css`, `js/omi-zone-overview.js`) are theme static files; Chart.js itself is SISTER's
self-hosted copy, passed as `chart_js_url`. The route (`web.web_omi_valuation`) only supplies the data: `overview`,
`chart_data` (`overview_chart_data`), `omi_url`, `omi_method_url` (`/valuation`) and `chart_js_url`. To change the layout,
edit the theme and bump it in `uv.lock`. Without a zone the page lists the comune's zones of the newest release with a link
to each overview. Tests: `tests/test_omi_zone_overview.py`.

## Limits to keep in mind

- Not an appraisal. Superficie catastale is a proxy for the surface the OMI quote refers to.
- The zone depends on the parcel being on the local map; an unmapped parcel gets no OMI estimate.
- Quotes are zone averages for ordinary condition; floor, exposure, real condition, pertinenze and constraints are ignored.
- Rivalutazioni and moltiplicatori follow the current IMU rules; update the constants if they change.

## Adding things

- A category's OMI typology: edit `OMI_TYPOLOGY_BY_CATEGORY` (and `OMI_TYPOLOGY_LABELS` for a new code). The page and its
  "unvalued" table follow; keep `OMI_UNVALUED_CATEGORIES` in step (the test checks it).
- A multiplier: edit `building_multiplier` and `BUILDING_MULTIPLIER_TABLE` together.
- A land market estimate: needs a real source of agricultural values (for example valori agricoli medi); the OMI database
  cannot provide it. Add a module next to `omi_quotes.py` and a separate column, never a change to the cadastral value.

## Tests

```bash
/opt/venv/.aecs4u_venv/bin/python -m pytest -p no:logfire -q --no-cov \
  tests/test_cadastral_values.py tests/test_omi_quotes.py tests/test_land_registry_kpis.py tests/test_valuation_docs.py
```

`test_land_registry_kpis.py` stubs the zone lookup and the OMI database; nothing needs a running service. Verify the page
itself with a real document (a subject visura with mapped fabbricati) after changing the columns: the unit tests do not
run the browser script.
