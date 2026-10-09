# Registro delle modifiche

Tutte le modifiche rilevanti a questo progetto saranno documentate in questo file.

Il formato è basato su [Keep a Changelog](https://keepachangelog.com/it/1.1.0/),
e questo progetto aderisce al [Versionamento Semantico](https://semver.org/lang/it/).

## [Non rilasciato]

### Aggiunto (2026-10-09)
- Flusso a due fasi per `search` / `intestati` (`run_visura`): prima si leggono **tutte le pagine HTML** (lista immobili e
  pagina Intestati di ogni immobile, senza CAPTCHA), poi si **richiedono i documenti** (unica pagina con CAPTCHA). Un CAPTCHA
  non risolto non fa piu' perdere gli intestati degli immobili successivi; `documents_pending` elenca le richieste mancanti.
  Una sola *Visura per Soggetto* per intestato (mai lo stesso codice fiscale due volte nella stessa esecuzione).
  Nuovo parametro `richiedi_documenti` (default `true`; `false` = solo HTML). Flowchart: `docs/property_visura_workflow.svg`,
  `docs/person_search_workflow.svg` (rigenerato)
- `sister/result_parsers.py`: parser puri per le celle composite del portale (sezione/foglio, diritto + quota, nominativo con
  luogo e data di nascita, `visImmSel`, radio degli intestati) e per sesso/codice di nascita dal codice fiscale
- `sister/xml_ingest.py`: i documenti XML (`VisuraFabbricati*`, `VisuraTerreni*`, `VisuraSoggettoAttuale`) popolano le tabelle
  tipizzate (`building_*`, `land_*`, `property_groups`, `ownership_mutations`, `property_owners`, `document_subjects`,
  intestazione in `document_metadata`), prima tutte vuote; `sister db backfill [projections|xml]` ricostruisce i dati gia' salvati
- `docs/data_extraction.md`: cosa restituisce ogni form e dove finisce nel database

### Corretto (2026-10-09)
- `visura_owners` e `visura_properties` non avevano mai `result_id`/`owner_index`: ora ogni intestato e' legato al proprio immobile
  (le viste owner↔property non incrociano piu' tutti gli intestati con tutti gli immobili di una risposta)
- Terreni: `area`, `dominical_income`, `agricultural_income` non venivano mai valorizzati (il portale usa `ha/are/ca` e
  `Reddito dominicale/agrario`); `_tipo_catasto`/`Catasto` di ogni riga decidono tipo e catasto dell'immobile
- Liste: `RA/103` diviso in sezione + foglio, `Subalterno`/`Zona` (elenco), `Ubicazione`, `Classamento`, importi senza `R.Euro:`;
  le righe di soggetto non producono piu' una sola `cadastral_location` senza comune
- `cadastral_subjects`: data/luogo di nascita, sesso e tipo vengono salvati e completano un soggetto creato in precedenza
  con meno dati; `parse_table` non restituisce piu' la colonna vuota del radio e conserva il valore del radio (`visImmSel`)
- `visura_documents.response_id` non veniva mai impostato; XML oltre 50 kB troncati in `document_metadata.content`; le visure
  terreni (`VisuraTerreniAttuale/Storica`) non venivano riconosciute; gli intestati storici si mescolavano a quelli attuali

### Aggiunto (2026-10-06)
- Sessione portale in due tempi: `scripts/ade_login.py` esegue il login (e `--close` chiude una sessione rimasta aperta);
  il servizio si limita ad **agganciare** la sessione SISTER esistente (`attach_existing_session`), non fa più login
  all'avvio. Lo shutdown/reload in modalità CDP non fa logout; *Stop* nel pannello chiude la sessione sul server.
  Vedi `docs/portal_session.md`
- `sister/query_forms.py`: definizione unica di ogni query single-step (input del form SISTER → parametro, path API,
  metodo client, obbligatori). Da lì sono generati i comandi `sister query <cmd>` (un'opzione per ogni input del form),
  il batch CSV, i parametri di `/web/forms` e la rotta generica `POST /visura/{search_type}`; invio unico via
  `VisuraClient.submit`. Vedi `docs/query_forms.md`
- Nuovi comandi: `query visura-storica`, `query soggetto-documento`, `query soggetto-immobili` (con `--con-intestati`,
  `--azienda`); `query soggetto` accetta anche cognome e dati di nascita
- `scripts/build_dossier_graph.py`: grafo proprietari ↔ immobili fino a chiusura (riprendibile). Vedi `docs/dossier_graph.md`
- Fixture dei form del portale (`tests/fixtures/portal_forms/`) e dei risultati (`tests/fixtures/single_step/`),
  test `test_query_forms.py` / `test_single_step_fixtures.py`
- `_set_office`: selezione dell'ufficio (Nazionale/Provincia) con verifica dell'intestazione del portale
- Scelta dell'intestato (omonimo) e seconda pagina "Visura per Soggetto" nel flusso immobile; modulo `visura per
  immobile` con tipo visura selezionabile (Completa, Storica Analitica/Sintetica)
- Attesa CAPTCHA: tab in primo piano, campo selezionato, nuovo CAPTCHA dopo un codice errato (max 5 tentativi)

### Corretto (2026-10-06)
- Cache: risposte con `data` salvato come testo JSON causavano errore 500; le rotte soggetto/azienda/elenco
  restituivano un id mai salvato (404) sui risultati in cache; `--force` ignorato dai comandi generici
- Selettore comune (`comuneCat` / `denomComune`) per indirizzo, partita, originali, fiduciali…; `elenco` non
  passava il foglio e richiede un gruppo di categorie; `indirizzo` ora segue la seconda pagina; `riepilogo`
  invia il form; `partita` usa il campo `numPart`; `elaborato-planimetrico` usa la particella e il pulsante "Inoltra";
  errori di validazione del portale ("Il campo … è obbligatorio") ora falliscono invece di restituire 0 risultati
- Rotta `ispezioni-cartacee` (404), argomento `target_index` di `intestati`, `esegui_generic` con argomenti duplicati,
  `VisuraService.download_richieste_documents` mancante; i documenti scaricati vanno in `SISTER_FILES_BASE`
- `sister query indirizzo --sezione` non veniva inviato
- Compilazione dei form: gli input validi solo in una modalità (identificativo provvisorio, richiesta storica, ricerca per cognome, registro particolare/generale) passano alla modalità richiesta; input assenti/nascosti o opzioni inesistenti falliscono subito con un messaggio chiaro invece di attendere il timeout del portale

### Aggiunto
- Licenza AGPL v3 (passaggio da GPL v3 per chiudere la SaaS loophole)
- File CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md
- Configurazione CI con GitHub Actions
- File pyproject.toml con metadati del progetto
- File .env.example completo con tutte le variabili
- Endpoint API per estrazione immobili (`POST /visura`) — accetta parametri catastali diretti
- Endpoint API per estrazione intestati (`POST /visura/intestati`) — accetta parametri catastali diretti
- Endpoint API per consultazione risultati (`GET /visura/{request_id}`)
- Endpoint API per estrazione sezioni territoriali (`POST /sezioni/extract`)
- Gestione automatica della sessione SISTER con keep-alive
- Ri-autenticazione automatica alla scadenza della sessione
- Shutdown graceful con logout dal portale
- Supporto Docker con docker-compose
- Filtro automatico immobili con partita "Soppressa"
- Gestione risultati multipli con iterazione radio button
- Nota sulla compatibilità SPID (solo CIE Sign / Sielte ID)

### Rimosso
- Rimossa dipendenza da PostgreSQL / SQLAlchemy — il servizio ora è completamente stateless
- Rimosso modulo `database.py`
- Rimossi endpoint che richiedevano il database (`GET /parcel/{parcel_fid}`, `GET /sezioni`, `GET /sezioni/stats`, `GET /sezioni/province`, `GET /sezioni/comuni/{provincia}`)
- Rimosse dipendenze `sqlalchemy` e `psycopg[binary]`

### Corretto
- Rimosso `sys.exit(0)` duplicato nel gestore dei segnali
