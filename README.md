# Visura API

[![Licenza](https://img.shields.io/badge/Licenza-AGPL%20v3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-green.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)

Servizio REST per l'estrazione automatizzata di dati catastali dal portale **SISTER** dell'Agenzia delle Entrate, con CLI integrata. Utilizza [`aecs4u-auth`](https://github.com/aecs4u/aecs4u-auth) per l'autenticazione SPID/CIE via browser headless e [FastAPI](https://fastapi.tiangolo.com/) per esporre gli endpoint.

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

Visura API permette di interrogare i dati catastali italiani tramite una semplice interfaccia HTTP o una CLI dedicata. Il flusso operativo è diviso in due fasi:

| Fase | Endpoint | CLI | Descrizione |
|------|----------|-----|-------------|
| **1 — Immobili** | `POST /visura` | `visura-api search` | Cerca gli immobili associati a foglio + particella |
| **2 — Intestati** | `POST /visura/intestati` | `visura-api intestati` | Recupera i titolari di uno specifico subalterno |

Entrambe le richieste vengono accodate ed eseguite sequenzialmente su un singolo browser autenticato al portale SISTER. I risultati si recuperano in polling con `GET /visura/{request_id}` o con `visura-api wait`.

### Funzionalità principali

- **CLI integrata** — `visura-api` con 7 subcomandi: search, intestati, get, wait, history, health, queries
- **Client Python** — `VisuraClient` asincrono con polling automatico e timeout configurabili
- **Autenticazione SPID automatizzata** via provider Sielte ID (CIE Sign) con push notification
- **Coda sequenziale** — le richieste vengono processate una alla volta per non sovraccaricare il portale
- **Database SQLite** — persistenza richieste e risposte con query storico
- **Ri-autenticazione automatica** — alla scadenza della sessione, il servizio tenta prima un recovery diretto e, solo se necessario, un nuovo login SPID
- **Keep-alive** — la sessione viene mantenuta attiva con un light keep-alive ogni 30 secondi e un refresh profondo ogni 5 minuti
- **Graceful shutdown** — su `SIGINT`/`SIGTERM` il servizio effettua il logout dal portale prima di chiudere il browser
- **Logging HTML completo** — ogni pagina visitata dal browser viene salvata su disco per debug e audit
- **Docker-ready** — immagine pronta con tutte le dipendenze di sistema per Chromium headless

### Compatibilità SPID

L'autenticazione SPID/CIE è gestita dal pacchetto [`aecs4u-auth`](https://github.com/aecs4u/aecs4u-auth), che supporta diversi provider (Sielte, Aruba, Poste, Namirial) e metodi di autenticazione (SPID, CIE, CNS, Fisconline). Il provider e il metodo si configurano tramite variabili d'ambiente (`ADE_AUTH_METHOD`, `ADE_SPID_PROVIDER`). Di default è configurato Sielte ID con approvazione via push notification sull'app MySielteID.

### Limitazioni note

- Alcune città presentano strutture catastali particolari (sezioni urbane, mappe speciali) che possono causare risultati parziali.
- Se la particella non esiste nel catasto, il portale restituisce "NESSUNA CORRISPONDENZA TROVATA" e l'API ritorna una lista vuota con il campo `error` valorizzato.
- Gli immobili con partita "Soppressa" vengono inclusi nei risultati ma senza intestati.

---

## Architettura

```
Client HTTP / CLI
     │
     ▼
┌──────────────────────────────────────────────────────┐
│  FastAPI  (visura_api/main.py)                       │
│                                                      │
│  ┌─────────────┐  ┌──────────────────────────────┐   │
│  │ Routes      │──│ VisuraService                │   │
│  │ (routes.py) │  │  • asyncio.Queue             │   │
│  └─────────────┘  │  • response_store (dict)     │   │
│                   │  • worker sequenziale        │   │
│  ┌─────────────┐  └──────────┬───────────────────┘   │
│  │ Models      │             │                       │
│  │ (models.py) │  ┌──────────▼───────────────────┐   │
│  └─────────────┘  │ BrowserManager               │   │
│                   │  → delega a aecs4u_auth       │   │
│  ┌─────────────┐  │  • SPID/CIE login + SISTER   │   │
│  │ Database    │  │  • Keep-alive, recovery       │   │
│  │ (SQLite)    │  └──────────┬───────────────────┘   │
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
visura-api/
├── visura_api/             # Codice sorgente
│   ├── main.py             # App FastAPI, lifespan, dependency injection
│   ├── routes.py           # Route handler functions
│   ├── services.py         # BrowserManager, VisuraService (coda + worker)
│   ├── models.py           # Pydantic input models, dataclass, eccezioni
│   ├── database.py         # SQLite persistence layer (aiosqlite)
│   ├── utils.py            # Automazione SISTER: run_visura(), parse_table()
│   ├── client.py           # VisuraClient — async HTTP client con polling
│   └── cli.py              # CLI Typer con 7 subcomandi
├── tests/                  # Test suite (140 test)
│   ├── conftest.py         # Fixtures e stub dipendenze
│   ├── test_database.py    # Test SQLite layer
│   ├── test_client.py      # Test HTTP client
│   ├── test_cli.py         # Test CLI commands
│   ├── test_models.py      # Test Pydantic validators
│   ├── test_fixtures.py    # Test endpoint fixtures
│   └── test_main_cache_and_states.py  # Test cache, TTL, queue, worker
├── examples/               # Esempi d'uso
│   ├── cli_usage.sh        # Tutti i comandi CLI con spiegazioni
│   ├── client_usage.py     # Script Python con VisuraClient
│   ├── login_and_visura.py # Browser automation diretta
│   └── login_and_intestati.py  # Flusso a due fasi con browser
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
git clone https://github.com/aecs4u/visura-api.git
cd visura-api

cp .env.example .env
# Modifica .env con le tue credenziali SPID

docker-compose up -d

# Verifica che il servizio sia attivo
visura-api health
# oppure: curl http://localhost:8000/health
```

### Installazione manuale

```bash
git clone https://github.com/aecs4u/visura-api.git
cd visura-api

python -m venv .venv
source .venv/bin/activate

# Con uv (raccomandato) — risolve automaticamente aecs4u-auth locale
uv sync

# Oppure con pip
pip install -e .
playwright install chromium

cp .env.example .env
# Modifica .env con le tue credenziali SPID

uvicorn visura_api.main:app --host 0.0.0.0 --port 8000
```

> **Nota:** `aecs4u-auth` è pubblicato su Google Artifact Registry. Per installazione locale in sviluppo, usa `pip install -e ../aecs4u-auth[browser]` oppure `uv sync` (il `pyproject.toml` include già la source locale per uv).

All'avvio il servizio:

1. Lancia un browser Chromium headless
2. Esegue il login SPID — **approva la notifica push** sull'app MySielteID entro 120 secondi
3. Naviga fino alla sezione Visure catastali del portale SISTER
4. Avvia il keep-alive e il worker della coda
5. Inizia ad accettare richieste su porta 8000

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
| `VISURA_API_URL` | `http://localhost:8000` | URL base del servizio |
| `VISURA_API_KEY` | — | Valore per l'header `X-API-Key` |
| `VISURA_API_TIMEOUT` | `30` | Timeout HTTP in secondi |
| `VISURA_POLL_INTERVAL` | `5` | Secondi tra un poll e l'altro |
| `VISURA_POLL_TIMEOUT` | `300` | Tempo massimo di attesa (secondi) |

---

## CLI

Dopo l'installazione (`pip install -e .`), il comando `visura-api` è disponibile:

```bash
visura-api --help
```

### Comandi disponibili

| Comando | Descrizione |
|---------|-------------|
| `visura-api search` | Cerca immobili su una particella (Fase 1) |
| `visura-api intestati` | Cerca intestati di un immobile (Fase 2) |
| `visura-api get <id>` | Recupera il risultato di una richiesta |
| `visura-api wait <id>` | Attende il completamento di una richiesta |
| `visura-api history` | Consulta lo storico delle visure |
| `visura-api health` | Controlla lo stato del servizio |
| `visura-api queries` | Elenca gli endpoint API disponibili |

### Ricerca immobili

```bash
# Ricerca fabbricati a Trieste — attende il risultato
visura-api search \
    --provincia Trieste \
    --comune TRIESTE \
    --foglio 9 \
    --particella 166 \
    --tipo-catasto F \
    --wait

# Ricerca Terreni + Fabbricati (ometti --tipo-catasto)
visura-api search -P Roma -C ROMA -F 100 -p 50 --wait

# Anteprima senza invio
visura-api search -P Trieste -C TRIESTE -F 9 -p 166 --dry-run

# Salva risultati su file
visura-api search -P Trieste -C TRIESTE -F 9 -p 166 --wait --output risultati.json
```

### Intestati

```bash
# Fabbricati (subalterno obbligatorio)
visura-api intestati \
    -P Trieste -C TRIESTE -F 9 -p 166 \
    -t F -sub 3 --wait

# Terreni (senza subalterno)
visura-api intestati \
    -P Roma -C ROMA -F 100 -p 50 \
    -t T --wait
```

### Polling manuale

```bash
# Invia e recupera il request_id
visura-api search -P Trieste -C TRIESTE -F 9 -p 166 -t F

# Controlla lo stato
visura-api get req_F_abc123

# Attendi con timeout personalizzato
visura-api wait req_F_abc123 --timeout 600 --interval 3
```

### Storico e salute

```bash
# Ultime 20 visure filtrate per provincia
visura-api history --provincia Trieste --limit 20

# Stato del servizio
visura-api health
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
curl -X POST http://localhost:8000/visura \
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
visura-api search -P Trieste -C TRIESTE -F 9 -p 166 -t F
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
curl -X POST http://localhost:8000/visura/intestati \
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
visura-api intestati -P Trieste -C TRIESTE -F 9 -p 166 -t F -sub 3
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
curl -s http://localhost:8000/visura/req_F_abc123 | jq .

# CLI — singolo poll
visura-api get req_F_abc123

# CLI — attesa automatica con timeout
visura-api wait req_F_abc123 --timeout 600
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
curl -s "http://localhost:8000/visura/history?provincia=Trieste&limit=20" | jq .

# CLI
visura-api history --provincia Trieste --limit 20
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
curl -X POST http://localhost:8000/shutdown \
  -H "X-API-Key: ${SHUTDOWN_API_KEY}"
```

---

## Client Python

Il modulo `client.py` fornisce un client asincrono riutilizzabile in script e applicazioni:

```python
import asyncio
from visura_api.client import VisuraClient

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
visura-api search -P Roma -C ROMA -F 100 -p 50 -t F --wait --output immobili.json

# 2. Prendi un subalterno dai risultati e cerca intestati
visura-api intestati -P Roma -C ROMA -F 100 -p 50 -t F -sub 3 --wait

# 3. Consulta lo storico
visura-api history --provincia Roma --limit 10
```

### Flusso completo con cURL

```bash
# 1. Avvia l'estrazione dei fabbricati
curl -s -X POST http://localhost:8000/visura \
  -H "Content-Type: application/json" \
  -d '{"provincia":"Roma","comune":"ROMA","foglio":"100","particella":"50","tipo_catasto":"F"}' \
  | jq .

# 2. Polling risultati (ripeti fino a status != "processing")
curl -s http://localhost:8000/visura/req_F_abc123 | jq .

# 3. Chiedi gli intestati per un subalterno specifico
curl -s -X POST http://localhost:8000/visura/intestati \
  -H "Content-Type: application/json" \
  -d '{"provincia":"Roma","comune":"ROMA","foglio":"100","particella":"50","tipo_catasto":"F","subalterno":"3"}' \
  | jq .

# 4. Polling intestati
curl -s http://localhost:8000/visura/intestati_F_xyz789 | jq .
```

### Scripting con CLI e jq

```bash
# Invia ricerca, estrai gli ID, attendi ognuno
visura-api search -P Trieste -C TRIESTE -F 9 -p 166 2>/dev/null \
  | jq -r '.request_ids[]' \
  | while read -r rid; do
      visura-api wait "$rid" --output "result_${rid}.json"
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
LOG_LEVEL=DEBUG uvicorn visura_api.main:app --host 0.0.0.0 --port 8000
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
git clone https://github.com/aecs4u/visura-api.git
cd visura-api

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
# Tutti i 140 test
python -m pytest

# Con output verbose
python -m pytest -v

# Solo un modulo
python -m pytest tests/test_database.py

# Con coverage
python -m pytest --cov=visura_api
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
| Risposte lente | Coda piena | Controlla `queue_size` con `visura-api health` |
| Chromium non si avvia in Docker | Dipendenze di sistema mancanti | Usa il Dockerfile fornito che include tutte le librerie necessarie |
| CLI non trova il servizio | URL sbagliato | Imposta `VISURA_API_URL` nel `.env` o via env var |

Per debug approfondito, ispeziona i file HTML in `logs/pages/` — mostrano esattamente cosa vedeva il browser in ogni step.

---

## Autore

Sviluppato da [zornade](https://zornade.com).

---

## Licenza

Distribuito sotto licenza [GNU Affero General Public License v3.0](LICENSE).
