# SISTER

[![Licenza](https://img.shields.io/badge/Licenza-AGPL%20v3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-green.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)

Servizio REST + CLI + Web UI per l'estrazione automatizzata di dati catastali dal portale **SISTER** dell'Agenzia delle Entrate. Utilizza [`aecs4u-auth`](https://github.com/aecs4u/aecs4u-auth) per l'autenticazione SPID/CIE via browser headless, [`aecs4u-theme`](https://github.com/aecs4u/aecs4u-theme) per l'interfaccia web e [FastAPI](https://fastapi.tiangolo.com/) per l'API REST.

> **Disclaimer legale** — Questo progetto è uno strumento indipendente e **non** è affiliato, approvato o supportato dall'Agenzia delle Entrate. L'utente è l'unico responsabile del rispetto dei termini di servizio del portale SISTER e della normativa vigente. L'uso di automazione sul portale potrebbe violare i termini d'uso del servizio.

> [!WARNING]  
> Per poter attivare le API bisogna **prima** registrarsi e chiedere l'accesso ai servizi sister utilizzando l'Area Personale di Agenzia delle Entrate e poi cercando "sister" tra i servizi disponibili. L'operazione è veloce.

---

## Indice

- [Panoramica](#panoramica)
- [Architettura](#architettura)
- [Prerequisiti](#prerequisiti)
- [Avvio rapido](#avvio-rapido)
- [Configurazione](#configurazione)
- [Web UI](#web-ui)
- [CLI](#cli)
- [Endpoint API](#endpoint-api)
  - [Health check](#health-check)
  - [Visura immobili (Fase 1)](#visura-immobili-fase-1)
  - [Visura intestati (Fase 2)](#visura-intestati-fase-2)
  - [Polling risultati](#polling-risultati)
  - [Storico visure](#storico-visure)
  - [Sezioni territoriali](#sezioni-territoriali)
  - [Shutdown](#shutdown)
- [Client Python](#client-python)
- [Esempi d'uso](#esempi-duso)
- [Logging e debug](#logging-e-debug)
- [Dettagli tecnici](#dettagli-tecnici)
- [Sviluppo e contribuzione](#sviluppo-e-contribuzione)
- [Risoluzione dei problemi](#risoluzione-dei-problemi)
- [Autore](#autore)
- [Licenza](#licenza)

---

## Panoramica

SISTER permette di interrogare i dati catastali italiani tramite una semplice interfaccia HTTP o una CLI dedicata. Il flusso operativo è diviso in due fasi:

| Fase | Endpoint | CLI | Descrizione |
|------|----------|-----|-------------|
| **1 — Immobili** | `POST /visura` | `sister query search` | Cerca gli immobili associati a foglio + particella |
| **2 — Intestati** | `POST /visura/intestati` | `sister query intestati` | Recupera i titolari di uno specifico subalterno |

Entrambe le richieste vengono accodate ed eseguite sequenzialmente su un singolo browser autenticato al portale SISTER. I risultati si recuperano in polling con `GET /visura/{request_id}` o con `sister wait`.

### Funzionalità principali

- **Web UI** — Dashboard, query forms (12 single-step + 10 workflow), results browser, workflow flowcharts with depth selector (light/standard/deep/full), batch CSV/JSON/XLSX upload
- **CLI integrata** — 25+ comandi query (incl. ispezioni ipotecarie), 10 workflow presets (7 standard + 3 multi-hop), batch, requests, history, db management
- **Client Python** — `VisuraClient` asincrono con polling automatico e timeout configurabili
- **Cache intelligente** — evita richieste duplicate; bypass con `--force`
- **Database SQLModel** — tabelle strutturate (immobili, intestati) + Alembic migrations
- **Autenticazione SPID/CIE** via [`aecs4u-auth`](https://github.com/aecs4u/aecs4u-auth) con keep-alive e recovery automatico
- **Coda sequenziale** — le richieste vengono processate una alla volta per non sovraccaricare il portale
- **Graceful shutdown** — su `SIGINT`/`SIGTERM` il servizio effettua il logout dal portale
- **Logging HTML completo** — ogni pagina visitata dal browser viene salvata su disco per debug e audit
- **Docker-ready** — immagine pronta con tutte le dipendenze di sistema per Chromium headless

### Compatibilità SPID

L'autenticazione SPID/CIE è gestita dal pacchetto [`aecs4u-auth`](https://github.com/aecs4u/aecs4u-auth), che supporta diversi provider (Sielte, Aruba, Poste, Namirial) e metodi di autenticazione (SPID, CIE, CNS, Fisconline). Il provider e il metodo si configurano tramite variabili d'ambiente (`ADE_AUTH_METHOD`, `ADE_SPID_PROVIDER`). Di default è configurato Sielte ID con approvazione via push notification sull'app MySielteID.

### Limitazioni note

- Alcune città presentano strutture catastali particolari (sezioni urbane, mappe speciali) che possono causare risultati parziali.
- Se la particella non esiste nel catasto, il portale restituisce "NESSUNA CORRISPONDENZA TROVATA" e l'API ritorna una lista vuota con il campo `error` valorizzato.
- Gli immobili con partita "Soppressa" vengono inclusi nei risultati ma senza intestati.
- **`query mappa`** (EM): la pagina Mappa ha un layout form diverso dagli altri — il selettore del pulsante di invio non corrisponde. Necessita ispezione HTML.
- **`query ispezioni`** / **`query ispezioni-cartacee`** (ISP/ISPCART): "Passa a Ispezioni" apre un modulo SISTER completamente diverso che richiede un flusso di navigazione dedicato.

---

## Architettura

```
Browser / CLI / API Client
     │
     ▼
┌──────────────────────────────────────────────────────┐
│  FastAPI  (sister/main.py)                           │
│                                                      │
│  ┌─────────────┐  ┌──────────────────────────────┐   │
│  │ Web UI      │  │ VisuraService                │   │
│  │ (web.py)    │  │  • asyncio.Queue             │   │
│  │ aecs4u-theme│  │  • cache + response_store    │   │
│  └─────────────┘  │  • worker sequenziale        │   │
│                   └──────────┬───────────────────┘   │
│  ┌─────────────┐             │                       │
│  │ API Routes  │  ┌──────────▼───────────────────┐   │
│  │ (routes.py) │  │ BrowserManager               │   │
│  └─────────────┘  │  → delega a aecs4u_auth      │   │
│                   │  • SPID/CIE login + SISTER   │   │
│  ┌─────────────┐  │  • Keep-alive, recovery      │   │
│  │ SQLModel DB │  └──────────┬───────────────────┘   │
│  │ (SQLite)    │             │                       │
│  └─────────────┘             │                       │
└──────────────────────────────┼───────────────────────┘
                               │
                               ▼
                ┌──────────────────────────┐
                │ Portale SISTER           │
                │ sister3.agenziaentrate   │
                │ .gov.it                  │
                └──────────────────────────┘
```

### Struttura del progetto

```
sister/
├── sister/                 # Codice sorgente (Python package)
│   ├── main.py             # App FastAPI, lifespan, theme, auth
│   ├── web.py              # Web UI routes + API proxy
│   ├── routes.py           # REST API route handlers
│   ├── services.py         # BrowserManager, VisuraService (coda + worker + cache)
│   ├── models.py           # Pydantic input models, dataclass, eccezioni
│   ├── db_models.py        # SQLModel ORM table classes
│   ├── database.py         # Async SQLAlchemy engine, sessions, cache
│   ├── form_config.py      # Web UI form group definitions
│   ├── utils.py            # Automazione SISTER: run_visura(), parse_table()
│   ├── client.py           # VisuraClient — async HTTP client con polling
│   ├── cli.py              # CLI Typer: 16+ query commands, workflow, batch, db
│   ├── templates/sister/   # Jinja2 templates (extends aecs4u-theme)
│   │   ├── landing.html    # Landing page (pubblica)
│   │   ├── index.html      # Dashboard
│   │   ├── forms.html      # Query forms (8 gruppi)
│   │   ├── results.html    # Results browser
│   │   └── ...
│   └── static/             # CSS, JS, icons, workflow SVG flowcharts
├── tests/                  # Test suite (158+ test)
├── alembic/                # Database migrations
├── data/                   # SQLite database (sister.sqlite)
├── examples/               # CLI + Python client examples
├── scripts/                # Start script
├── docs/                   # Governance docs
├── Dockerfile
├── docker-compose.yaml
├── pyproject.toml
└── .env.example
```

---

## Prerequisiti

- **Python 3.11+** (testato fino a 3.13)
- **Credenziali SPID** tramite provider Sielte ID con app MySielteID configurata
- **Convenzione SISTER attiva** — l'utente deve avere un account abilitato sul portale SISTER

Per Docker:
- Docker Engine 20+
- Docker Compose v2

---

## Avvio rapido

### Con Docker (raccomandato)

```bash
git clone https://github.com/aecs4u/sister.git
cd sister

cp .env.example .env
# Modifica .env con le tue credenziali SPID

docker-compose up -d

# Verifica che il servizio sia attivo
uv run sister health
# oppure: curl http://localhost:8025/health
```

### Installazione manuale

```bash
git clone https://github.com/aecs4u/sister.git
cd sister

python -m venv .venv
source .venv/bin/activate

# Con uv (raccomandato) — risolve automaticamente aecs4u-auth locale
uv sync

# Oppure con pip
pip install -e .
playwright install chromium

cp .env.example .env
# Modifica .env con le tue credenziali SPID

./scripts/start.sh
```

> **Nota:** `aecs4u-auth` e `aecs4u-theme` sono su GitHub. Il `pyproject.toml` include le source git per uv.

All'avvio il servizio:

1. Lancia un browser Chromium headless
2. Esegue il login SPID — **approva la notifica push** sull'app MySielteID entro 120 secondi
3. Naviga fino alla sezione Visure catastali del portale SISTER
4. Avvia il keep-alive e il worker della coda
5. Inizia ad accettare richieste su porta 8025

---

## Configurazione

Crea un file `.env` nella root del progetto (vedi `.env.example`):

```env
# Obbligatorio — Credenziali SPID / Agenzia delle Entrate
ADE_USERNAME=RSSMRA85M01H501Z    # Codice fiscale
ADE_PASSWORD=la_tua_password

# Opzionale — Autenticazione (gestite da aecs4u-auth)
ADE_AUTH_METHOD=spid              # spid | cie | cns | fisconline
ADE_SPID_PROVIDER=sielte          # sielte | aruba | poste | namirial

# Opzionale — Applicazione
API_KEY=una_chiave_operativa       # Protegge endpoint operativi via X-API-Key
LOG_LEVEL=INFO                    # DEBUG | INFO | WARNING | ERROR
SHUTDOWN_API_KEY=una_chiave_lunga # Protegge POST /shutdown via header X-API-Key
QUEUE_MAX_SIZE=100                # Capienza massima coda richieste
RESPONSE_TTL_SECONDS=21600        # TTL cache risultati (default 6 ore)
RESPONSE_MAX_ITEMS=5000           # Massimo risultati in memoria
RESPONSE_CLEANUP_INTERVAL_SECONDS=60 # Intervallo cleanup cache (secondi)
```

### Variabili server

| Variabile | Obbligatoria | Default | Descrizione |
|-----------|:------------:|---------|-------------|
| `ADE_USERNAME` | ✅ | — | Codice fiscale per il login SPID |
| `ADE_PASSWORD` | ✅ | — | Password SPID |
| `ADE_AUTH_METHOD` | | `spid` | Metodo di autenticazione: `spid`, `cie`, `cns`, `fisconline` |
| `ADE_SPID_PROVIDER` | | `sielte` | Provider SPID: `sielte`, `aruba`, `poste`, `namirial` |
| `ADE_OTP_SECRET` | | — | Secret TOTP base32 (per provider con OTP) |
| `BROWSER_HEADLESS` | | `true` | Esegui browser in modalità headless |
| `BROWSER_MFA_TIMEOUT` | | `120` | Timeout in secondi per approvazione MFA |
| `API_KEY` | | non impostata | Se impostata, richiede `X-API-Key` sugli endpoint operativi |
| `LOG_LEVEL` | | `INFO` | Livello di log su console e file |
| `SHUTDOWN_API_KEY` | | non impostata | Se assente, endpoint `POST /shutdown` disabilitato |
| `QUEUE_MAX_SIZE` | | `100` | Numero massimo richieste accodabili prima di rispondere `429` |
| `RESPONSE_TTL_SECONDS` | | `21600` | Tempo massimo (secondi) di retention risultati in memoria |
| `RESPONSE_MAX_ITEMS` | | `5000` | Numero massimo di risultati mantenuti in cache |
| `RESPONSE_CLEANUP_INTERVAL_SECONDS` | | `60` | Frequenza cleanup periodico cache risultati |

### Variabili client (CLI e VisuraClient)

| Variabile | Default | Descrizione |
|-----------|---------|-------------|
| `VISURA_API_URL` | `http://localhost:8025` | URL base del servizio |
| `VISURA_API_KEY` | — | Valore per l'header `X-API-Key` |
| `VISURA_API_TIMEOUT` | `30` | Timeout HTTP in secondi |
| `VISURA_POLL_INTERVAL` | `5` | Secondi tra un poll e l'altro |
| `VISURA_POLL_TIMEOUT` | `300` | Tempo massimo di attesa (secondi) |

---

## Web UI

SISTER include un'interfaccia web accessibile al browser, basata su [`aecs4u-theme`](https://github.com/aecs4u/aecs4u-theme).

### Pagine disponibili

| URL | Descrizione |
|-----|-------------|
| `GET /` | Landing page (pubblica, senza autenticazione) |
| `GET /web/` | Dashboard — statistiche servizio, attività recente |
| `GET /web/forms` | 8 form di ricerca raggruppati per tipo |
| `GET /web/results` | Browser risultati con filtri e paginazione |
| `GET /web/results/{id}` | Dettaglio risultato: immobili + intestati |
| `GET /web/about` | Informazioni sul servizio |
| `GET /web/privacy` | Privacy policy |

### Form di ricerca

La pagina `/web/forms` include 8 gruppi di form:

1. **Property Search** — ricerca per foglio/particella + intestati
2. **Person Search** — ricerca nazionale per codice fiscale
3. **Company Search** — ricerca per P.IVA o denominazione
4. **Property List** — elenco immobili per comune
5. **Address Search** — ricerca per indirizzo
6. **Partita Search** — ricerca per partita catastale
7. **Workflow** — 10 preset con flowchart SVG interattivo (7 standard + 3 multi-hop: full-due-diligence, full-patrimonio, full-aziendale) e depth selector (light/standard/deep/full)
8. **Batch Upload** — drop zone per CSV, JSON o XLSX con anteprima e validazione tabella

I form inviano le richieste tramite proxy API (`POST /web/api/*`) e effettuano il polling dei risultati automaticamente.

---

## CLI

Dopo l'installazione (`uv sync` o `pip install -e .`), il comando `sister` è disponibile:

```bash
uv run sister --help
```

### Comandi disponibili

```
sister
├── query                              # Submit cadastral queries
│   ├── search                         # Immobili by foglio/particella
│   ├── intestati                      # Owners for a property
│   ├── soggetto                       # National search by codice fiscale
│   ├── azienda                        # Search by P.IVA or company name
│   ├── elenco                         # List properties in a comune
│   ├── indirizzo                      # Search by street address
│   ├── partita                        # Search by partita catastale
│   ├── nota                           # Search by annotation/note
│   ├── mappa                          # Cadastral map data (*)
│   ├── export-mappa                   # Export cadastral map (*)
│   ├── originali                      # Original registration records
│   ├── fiduciali                      # Survey reference points
│   ├── ispezioni                      # Inspection records (*)
│   ├── ispezioni-cartacee             # Paper inspection records (*)
│   ├── workflow                       # Multi-phase with presets
│   └── batch                          # Batch queries from CSV
├── get <request_id>                   # Poll a single result
├── wait <request_id>                  # Poll until complete
├── requests                           # List all requests with status
├── history                            # Query response history
├── health                             # Service health check
└── queries                            # List available endpoints

(*) = known limitations, see "Limitazioni note"
```

### Ricerca immobili

```bash
# Ricerca fabbricati a Trieste — attende il risultato
uv run sister query search \
    --provincia Trieste \
    --comune TRIESTE \
    --foglio 9 \
    --particella 166 \
    --tipo-catasto F \
    --wait

# Ricerca Terreni + Fabbricati (ometti --tipo-catasto)
uv run sister query search -P Roma -C ROMA -F 100 -p 50 --wait

# Anteprima senza invio
uv run sister query search -P Trieste -C TRIESTE -F 9 -p 166 --dry-run

# Salva risultati su file
uv run sister query search -P Trieste -C TRIESTE -F 9 -p 166 --wait --output risultati.json
```

### Intestati

```bash
# Fabbricati (subalterno obbligatorio)
uv run sister query intestati \
    -P Trieste -C TRIESTE -F 9 -p 166 \
    -t F -sub 3 --wait

# Terreni (senza subalterno)
uv run sister query intestati \
    -P Roma -C ROMA -F 100 -p 50 \
    -t T --wait
```

### Ricerca soggetto (codice fiscale)

```bash
# Ricerca nazionale — tutti gli immobili di una persona
uv run sister query soggetto --cf RSSMRI85E28H501E --wait

# Limitata a una provincia
uv run sister query soggetto --cf RSSMRI85E28H501E -P Roma --wait -o soggetto.json
```

### Ricerca azienda (P.IVA o denominazione)

```bash
# Per partita IVA
uv run sister query azienda --id 02471840997 --wait

# Per denominazione
uv run sister query azienda --id "TIGULLIO IMMOBILIARE SRL" -P Torino --wait
```

### Elenco immobili

```bash
# Tutti gli immobili di un comune
uv run sister query elenco -P Roma -C ROMA -t T --wait

# Filtrato per foglio
uv run sister query elenco -P Roma -C ROMA -F 100 --wait -o elenco.json
```

### Altri tipi di ricerca

```bash
# Per indirizzo
uv run sister query indirizzo -P Terni -C TERNI -a "DEL RIVO" --wait

# Per partita catastale
uv run sister query partita -P Roma -C ROMA --partita 12345 --wait

# Per nota/annotazione
uv run sister query nota -P Bologna --numero 5678 --anno 2024 --wait

# Originali di impianto
uv run sister query originali -P Bologna -C BOLOGNA -F 55 --wait

# Punti fiduciali
uv run sister query fiduciali -P Roma -C ROMA -F 100 --wait
```

### Workflow con preset

```bash
# Due diligence immobiliare — search → intestati → ispezioni
uv run sister query workflow --preset due-diligence \
    -P Roma -C ROMA -F 100 -p 50 -o due_diligence.json

# Indagine patrimoniale — tutti gli immobili di un soggetto
uv run sister query workflow --preset patrimonio \
    --cf RSSMRI85E28H501E -o patrimonio.json

# Mappatura fondiaria — elenco → mappa → fiduciali → originali
uv run sister query workflow --preset fondiario \
    -P Roma -C ROMA -F 100 -o fondiario.json

# Audit aziendale — tutti gli immobili di un'azienda
uv run sister query workflow --preset aziendale \
    --azienda 02471840997 -o audit.json

# Analisi storica — search → intestati → nota → ispezioni → originali
uv run sister query workflow --preset storico \
    -P Trieste -C TRIESTE -F 9 -p 166 -o storico.json

# Ricerca per indirizzo — indirizzo → search → intestati
uv run sister query workflow --preset indirizzo \
    -P Terni -C TERNI --indirizzo "DEL RIVO" -o indirizzo.json

# Controllo incrociato persona/azienda
uv run sister query workflow --preset cross-reference \
    --cf RSSMRI85E28H501E --azienda 02471840997 -o cross.json

# Workflow con depth "deep" — include owner expansion e ispezioni ipotecarie
uv run sister query workflow --preset due-diligence \
    -P Roma -C ROMA -F 100 -p 50 --depth deep --include-paid --yes -o deep.json

# Workflow con depth "light" — solo step core (veloce)
uv run sister query workflow --preset patrimonio \
    --cf RSSMRI85E28H501E --depth light -o quick.json

# Multi-hop full investigation — bounded graph expansion
uv run sister query workflow --preset full-due-diligence \
    -P Roma -C ROMA -F 100 -p 50 --depth full --include-paid --yes \
    --max-paid 5 --max-owners 15 --max-history 10 -o full_investigation.json

# Multi-hop corporate audit
uv run sister query workflow --preset full-aziendale \
    --azienda 02471840997 --depth full -o full_audit.json

# Workflow custom — combina flag a piacere
uv run sister query workflow \
    -P Roma -C ROMA -F 100 -p 50 \
    --cf RSSMRI85E28H501E --elenco --mappa \
    -o full_report.json
```

### Batch (ricerca multipla da CSV)

```bash
# Ricerca immobili da CSV
uv run sister query batch -I parcelle.csv --wait -O ./results/

# Ricerca soggetto da CSV
uv run sister query batch -I codici_fiscali.csv --command soggetto --wait

# Comandi misti in un CSV (colonna 'command' per riga)
uv run sister query batch -I mixed.csv --command auto --wait -o batch.json

# Anteprima
uv run sister query batch -I parcelle.csv --dry-run
```

Formato CSV (ricerca immobili):
```csv
provincia,comune,foglio,particella,tipo_catasto
Roma,ROMA,100,50,T
Roma,ROMA,100,50,F
```

Formato CSV (comandi misti):
```csv
command,provincia,comune,foglio,particella,codice_fiscale,identificativo,tipo_catasto
search,Roma,ROMA,100,50,,,T
soggetto,,,,,RSSMRI85E28H501E,,
azienda,,,,,,02471840997,
```

### Polling manuale

```bash
# Invia e recupera il request_id
uv run sister query search -P Trieste -C TRIESTE -F 9 -p 166 -t F

# Controlla lo stato
uv run sister get req_F_abc123

# Attendi con timeout personalizzato
uv run sister wait req_F_abc123 --timeout 600 --interval 3
```

### Requests, storico e salute

```bash
# Lista richieste con filtro stato
uv run sister requests --status pending
uv run sister requests --status completed --provincia Trieste

# Storico visure
uv run sister history --provincia Trieste --limit 20

# Stato del servizio
uv run sister health
```

Per altri esempi vedi [`examples/cli_usage.sh`](examples/cli_usage.sh).

---

## Endpoint API

### Health check

```
GET /health
```

```json
{
  "status": "healthy",
  "authenticated": true,
  "queue_size": 0,
  "pending_requests": 0,
  "cached_responses": 0,
  "response_ttl_seconds": 21600,
  "response_max_items": 5000,
  "queue_max_size": 100,
  "response_cleanup_interval_seconds": 60,
  "database": {
    "total_requests": 42,
    "total_responses": 40,
    "successful": 38,
    "failed": 2
  }
}
```

---

### Visura immobili (Fase 1)

```
POST /visura
```

Cerca tutti gli immobili su una particella catastale. Se `tipo_catasto` è omesso, vengono accodate **due** richieste (Terreni + Fabbricati).
Se `API_KEY` è configurata, richiede header `X-API-Key`.

**Request body:**

| Campo | Tipo | Obbligatorio | Default | Descrizione |
|-------|------|:------------:|---------|-------------|
| `provincia` | `string` | ✅ | — | Nome della provincia (es. `"Trieste"`) |
| `comune` | `string` | ✅ | — | Nome del comune (es. `"TRIESTE"`) |
| `foglio` | `string` | ✅ | — | Numero foglio |
| `particella` | `string` | ✅ | — | Numero particella |
| `sezione` | `string` | | `null` | Sezione censuaria (se presente) |
| `subalterno` | `string` | | `null` | Subalterno (opzionale, restringe la ricerca per fabbricati) |
| `tipo_catasto` | `string` | | `null` | `"T"` = Terreni, `"F"` = Fabbricati. Se omesso: entrambi |

**Esempio con curl:**

```bash
curl -X POST http://localhost:8025/visura \
  -H "Content-Type: application/json" \
  -d '{
    "provincia": "Trieste",
    "comune": "TRIESTE",
    "foglio": "9",
    "particella": "166",
    "tipo_catasto": "F"
  }'
```

**Esempio con CLI:**

```bash
uv run sister query search -P Trieste -C TRIESTE -F 9 -p 166 -t F
```

**Risposta:**

```json
{
  "request_ids": ["req_F_2f7f40f95cfb4bd8a8d8fe7b89612268"],
  "tipos_catasto": ["F"],
  "status": "queued",
  "message": "Richieste aggiunte alla coda per TRIESTE F.9 P.166"
}
```

---

### Visura intestati (Fase 2)

```
POST /visura/intestati
```

Estrae i titolari (intestati) di uno specifico immobile. Per i Fabbricati è necessario specificare il `subalterno`.
Se `API_KEY` è configurata, richiede header `X-API-Key`.

**Request body:**

| Campo | Tipo | Obbligatorio | Default | Descrizione |
|-------|------|:------------:|---------|-------------|
| `provincia` | `string` | ✅ | — | Nome della provincia |
| `comune` | `string` | ✅ | — | Nome del comune |
| `foglio` | `string` | ✅ | — | Numero foglio |
| `particella` | `string` | ✅ | — | Numero particella |
| `tipo_catasto` | `string` | ✅ | — | `"T"` o `"F"` |
| `subalterno` | `string` | Per `F` | `null` | Subalterno (obbligatorio per Fabbricati, vietato per Terreni) |
| `sezione` | `string` | | `null` | Sezione censuaria |

**Esempio:**

```bash
# curl
curl -X POST http://localhost:8025/visura/intestati \
  -H "Content-Type: application/json" \
  -d '{
    "provincia": "Trieste",
    "comune": "TRIESTE",
    "foglio": "9",
    "particella": "166",
    "tipo_catasto": "F",
    "subalterno": "3"
  }'

# CLI
uv run sister query intestati -P Trieste -C TRIESTE -F 9 -p 166 -t F -sub 3
```

**Risposta:**

```json
{
  "request_id": "intestati_F_9f3fa9cf2fcb49c6a8a21bf2312e3ef3",
  "tipo_catasto": "F",
  "subalterno": "3",
  "status": "queued",
  "message": "Richiesta intestati aggiunta alla coda per TRIESTE F.9 P.166",
  "queue_position": 1
}
```

---

### Polling risultati

```
GET /visura/{request_id}
```

Recupera lo stato e i dati di una richiesta precedentemente accodata.
Se `API_KEY` è configurata, richiede header `X-API-Key`.

| Status | Significato |
|--------|-------------|
| `processing` | La richiesta è in coda o in esecuzione |
| `completed` | Dati disponibili nel campo `data` |
| `error` | Errore — dettagli nel campo `error` |
| `expired` | Risultato non più disponibile (cache scaduta o evicted) |

Se `request_id` non esiste, l'endpoint risponde con `404`.
Se il risultato è scaduto, risponde con `410` e `status: "expired"`.

```bash
# curl
curl -s http://localhost:8025/visura/req_F_abc123 | jq .

# CLI — singolo poll
uv run sister get req_F_abc123

# CLI — attesa automatica con timeout
uv run sister wait req_F_abc123 --timeout 600
```

**Risposta completata (Fase 1):**

```json
{
  "request_id": "req_F_2f7f40f95cfb4bd8a8d8fe7b89612268",
  "tipo_catasto": "F",
  "status": "completed",
  "data": {
    "immobili": [
      {
        "Foglio": "9",
        "Particella": "166",
        "Sub": "3",
        "Categoria": "A/2",
        "Classe": "5",
        "Consistenza": "4.5",
        "Rendita": "500,00",
        "Indirizzo": "VIA ROMA 10",
        "Partita": "12345"
      }
    ],
    "results": [],
    "total_results": 1,
    "intestati": []
  },
  "error": null,
  "timestamp": "2026-03-06T10:30:00"
}
```

**Risposta completata (Fase 2 — intestati):**

```json
{
  "request_id": "intestati_F_9f3fa9cf2fcb49c6a8a21bf2312e3ef3",
  "status": "completed",
  "data": {
    "immobile": {
      "Foglio": "9",
      "Particella": "166",
      "Sub": "3"
    },
    "intestati": [
      {
        "Nominativo o denominazione": "ROSSI MARIO",
        "Codice fiscale": "RSSMRA85M01H501Z",
        "Titolarità": "Proprietà per 1/1"
      }
    ],
    "total_intestati": 1
  }
}
```

---

### Storico visure

```
GET /visura/history
```

Consulta lo storico delle visure salvate nel database SQLite.

| Parametro | Tipo | Default | Descrizione |
|-----------|------|---------|-------------|
| `provincia` | `string` | — | Filtra per provincia |
| `comune` | `string` | — | Filtra per comune |
| `foglio` | `string` | — | Filtra per foglio |
| `particella` | `string` | — | Filtra per particella |
| `tipo_catasto` | `string` | — | Filtra per tipo (`T`/`F`) |
| `limit` | `int` | `50` | Massimo risultati (max 200) |
| `offset` | `int` | `0` | Offset per paginazione |

```bash
# curl
curl -s "http://localhost:8025/visura/history?provincia=Trieste&limit=20" | jq .

# CLI
uv run sister history --provincia Trieste --limit 20
```

---

### Sezioni territoriali

```
POST /sezioni/extract
```

Estrae le sezioni censuarie per tutte le province e comuni d'Italia. **Operazione molto lenta** — può richiedere ore.
Se `API_KEY` è configurata, richiede header `X-API-Key`.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `tipo_catasto` | `string` | `"T"` | `"T"` o `"F"` |
| `max_province` | `int` | `200` | Numero massimo di province da processare (1–200) |

---

### Shutdown

```
POST /shutdown
```

Esegue un shutdown controllato: logout dal portale SISTER e chiusura del browser.
Richiede header `X-API-Key` uguale a `SHUTDOWN_API_KEY`.

```bash
curl -X POST http://localhost:8025/shutdown \
  -H "X-API-Key: ${SHUTDOWN_API_KEY}"
```

---

## Client Python

Il modulo `client.py` fornisce un client asincrono riutilizzabile in script e applicazioni:

```python
import asyncio
from sister.client import VisuraClient

async def main():
    client = VisuraClient()  # legge config da env vars

    # Controlla che il servizio sia attivo
    health = await client.health()
    print(f"Status: {health['status']}")

    # Cerca fabbricati
    result = await client.search(
        provincia="Trieste",
        comune="TRIESTE",
        foglio="9",
        particella="166",
        tipo_catasto="F",
    )
    request_id = result["request_ids"][0]

    # Attendi il risultato (poll automatico)
    response = await client.wait_for_result(request_id)
    immobili = response["data"]["immobili"]
    print(f"Trovati {len(immobili)} immobili")

    # Storico
    history = await client.history(provincia="Trieste", limit=10)
    print(f"{history['count']} visure nello storico")

asyncio.run(main())
```

Per un esempio completo con gestione errori, vedi [`examples/client_usage.py`](examples/client_usage.py).

---

## Esempi d'uso

### Flusso completo con CLI

```bash
# 1. Cerca fabbricati e attendi
uv run sister query search -P Roma -C ROMA -F 100 -p 50 -t F --wait --output immobili.json

# 2. Prendi un subalterno dai risultati e cerca intestati
uv run sister query intestati -P Roma -C ROMA -F 100 -p 50 -t F -sub 3 --wait

# 3. Oppure: workflow automatico (fase 1 + fase 2 in un solo comando)
uv run sister query workflow -P Roma -C ROMA -F 100 -p 50 -t F --output full.json

# 4. Batch da file CSV
uv run sister query batch -I parcelle.csv --wait -O ./results/

# 5. Consulta lo storico
uv run sister history --provincia Roma --limit 10
```

### Flusso completo con cURL

```bash
# 1. Avvia l'estrazione dei fabbricati
curl -s -X POST http://localhost:8025/visura \
  -H "Content-Type: application/json" \
  -d '{"provincia":"Roma","comune":"ROMA","foglio":"100","particella":"50","tipo_catasto":"F"}' \
  | jq .

# 2. Polling risultati (ripeti fino a status != "processing")
curl -s http://localhost:8025/visura/req_F_abc123 | jq .

# 3. Chiedi gli intestati per un subalterno specifico
curl -s -X POST http://localhost:8025/visura/intestati \
  -H "Content-Type: application/json" \
  -d '{"provincia":"Roma","comune":"ROMA","foglio":"100","particella":"50","tipo_catasto":"F","subalterno":"3"}' \
  | jq .

# 4. Polling intestati
curl -s http://localhost:8025/visura/intestati_F_xyz789 | jq .
```

### Scripting con CLI e jq

```bash
# Invia ricerca, estrai gli ID, attendi ognuno
uv run sister query search -P Trieste -C TRIESTE -F 9 -p 166 2>/dev/null \
  | jq -r '.request_ids[]' \
  | while read -r rid; do
      uv run sister wait "$rid" --output "result_${rid}.json"
    done
```

Per altri esempi vedi:
- [`examples/cli_usage.sh`](examples/cli_usage.sh) — tutti i comandi CLI commentati
- [`examples/client_usage.py`](examples/client_usage.py) — client Python con health check, search, intestati, history
- [`examples/login_and_visura.py`](examples/login_and_visura.py) — browser automation diretta
- [`examples/login_and_intestati.py`](examples/login_and_intestati.py) — flusso a due fasi con browser

---

## Logging e debug

Il servizio produce due livelli di logging:

### Log testuale

Scritto su **stdout** e su **file** in `logs/visura.log`. Contiene l'intero flusso operativo: login, navigazione, estrazione dati, errori.

```bash
# Avvia con log dettagliati
LOG_LEVEL=DEBUG ./scripts/start.sh
```

### Log HTML delle pagine (`PageLogger`)

Ogni pagina visitata dal browser viene salvata come file HTML su disco. Questo permette di ispezionare esattamente ciò che il browser ha visto in ogni punto del flusso — utile per debug, audit e sviluppo.

**Struttura directory:**

```
logs/pages/
└── 2026-03-06_16-28-24/          ← session_id (reset ad ogni avvio del server)
    ├── login/
    │   ├── 01_goto_login.html
    │   └── ...
    ├── visura/
    │   ├── 01_scelta_servizio.html
    │   ├── 02_provincia_applicata.html
    │   └── ...
    ├── logout/
    └── recovery/
```

> **Privacy:** la directory `logs/pages/` è nel `.gitignore` perché i file HTML contengono dati personali (codice fiscale, intestatari, indirizzi). Non committare mai questi file.

---

## Dettagli tecnici

### Gestione della sessione

| Meccanismo | Intervallo | Descrizione |
|------------|------------|-------------|
| **Light keep-alive** | 30 secondi | Mouse move sulla pagina per evitare timeout idle |
| **Session refresh** | 5 minuti | Naviga a `SceltaServizio.do` e verifica che la sessione sia ancora attiva |
| **Recovery** | Su errore | Navigazione diretta → percorso interno → re-login SPID completo |

### Coda di elaborazione

- Unica `asyncio.Queue` con worker sequenziale
- Pausa di **2 secondi** tra una richiesta e l'altra
- Pausa di **5 secondi** dopo un errore
- I risultati restano in memoria (`response_store`) e nel **database SQLite**
- Il client fa polling su `GET /visura/{request_id}` — restituisce `"processing"` finché il risultato non è pronto
- Se il risultato non è in cache, viene cercato automaticamente nel database

### Graceful shutdown

Quando uvicorn riceve `SIGINT` o `SIGTERM`:

1. Il lifespan `shutdown` viene invocato da uvicorn
2. `aecs4u-auth` effettua il logout dal portale SISTER
3. Il browser context e Chromium vengono chiusi

---

## Sviluppo e contribuzione

### Setup ambiente di sviluppo

```bash
git clone https://github.com/aecs4u/sister.git
cd sister

# Con uv (raccomandato) — risolve automaticamente aecs4u-auth locale
uv sync --extra dev
uv run playwright install chromium

# Oppure con pip
python -m venv .venv
source .venv/bin/activate
pip install -e ../aecs4u-auth[browser]   # dipendenza locale
pip install -e ".[dev]"                  # pytest, black, ruff
playwright install chromium

cp .env.example .env
# Configura le credenziali
```

### Test

```bash
# Tutti i 156 test
python -m pytest

# Con output verbose
python -m pytest -v

# Solo un modulo
python -m pytest tests/test_database.py

# Con coverage
python -m pytest --cov=sister
```

### Formattazione e linting

```bash
black .           # formattazione automatica
ruff check .      # controllo linting
```

### Docker

```bash
docker-compose up --build         # build e avvio
docker-compose logs -f             # segui i log
docker-compose down                # stop e rimozione container
```

### Cambiare provider SPID o metodo di autenticazione

Non serve modificare codice. Basta cambiare le variabili d'ambiente:

```env
ADE_AUTH_METHOD=spid          # oppure: cie, cns, fisconline
ADE_SPID_PROVIDER=aruba       # oppure: sielte, poste, namirial
```

Per aggiungere un provider non supportato, contribuisci al pacchetto [`aecs4u-auth`](https://github.com/aecs4u/aecs4u-auth).

### Linee guida

Leggi [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) per il dettaglio completo. In breve:

- Crea un branch dal `main` con un nome descrittivo (`fix/...`, `feat/...`)
- Ogni modifica significativa deve includere i log `PageLogger` nei punti critici
- **Mai committare** file da `logs/` — contengono dati personali
- Rimuovi le credenziali dai log prima di condividerli in una issue

---

## Risoluzione dei problemi

| Problema | Causa probabile | Soluzione |
|----------|----------------|----------|
| Il login non parte | Credenziali mancanti | Verifica `ADE_USERNAME` e `ADE_PASSWORD` nel file `.env` |
| Timeout su "Autorizza" | Push non approvata in tempo | Approva la notifica MySielteID entro 120 secondi |
| "Utente già in sessione" | Sessione precedente non chiusa | Attendi qualche minuto o chiudi manualmente dal portale |
| Sessione scaduta durante visura | Inattività prolungata | Il servizio tenta il recovery automatico; se fallisce, ri-esegue il login |
| "NESSUNA CORRISPONDENZA TROVATA" | Dati catastali inesistenti | Verifica foglio, particella, tipo catasto e comune |
| Risposte lente | Coda piena | Controlla `queue_size` con `uv run sister health` |
| Chromium non si avvia in Docker | Dipendenze di sistema mancanti | Usa il Dockerfile fornito che include tutte le librerie necessarie |
| CLI non trova il servizio | URL sbagliato | Imposta `VISURA_API_URL` nel `.env` o via env var |

Per debug approfondito, ispeziona i file HTML in `logs/pages/` — mostrano esattamente cosa vedeva il browser in ogni step.

---

## Autore

Sviluppato da [AECS4U Srl](https://aecs4u.com).

---

## Licenza

Distribuito sotto licenza [GNU Affero General Public License v3.0](LICENSE).
