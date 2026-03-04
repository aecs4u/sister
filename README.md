# Visura API

[![Licenza](https://img.shields.io/badge/Licenza-GPL%20v3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-green.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104-009688.svg)](https://fastapi.tiangolo.com/)

Servizio API per l'estrazione automatizzata di dati catastali dal portale **SISTER** (Servizi Integrati Telematici dell'Agenzia delle Entrate).

> **Disclaimer legale**: questo progetto è uno strumento indipendente e non è affiliato, approvato o supportato dall'Agenzia delle Entrate. L'utente è l'unico responsabile del rispetto dei termini di servizio del portale SISTER e della normativa vigente. L'uso di automazione sul portale potrebbe violare i termini d'uso del servizio.

> **Compatibilità SPID**: il login automatizzato funziona **esclusivamente** con il provider **CIE Sign / Sielte ID**. Altri provider SPID non sono supportati e richiederebbero modifiche al flusso di autenticazione in `utils.py`.

> **Limitazioni note**: alcune città presentano problemi con determinate mappe catastali o sezioni urbane. In questi casi l'estrazione potrebbe fallire o restituire dati incompleti. Il comportamento dipende dalla struttura dei dati sul portale SISTER, che varia da comune a comune.

## Indice

- [Panoramica](#panoramica)
- [Architettura](#architettura)
- [Compatibilità SPID](#compatibilità-spid)
- [Installazione](#installazione)
- [Configurazione](#configurazione)
- [Endpoint API](#endpoint-api)
- [Esempi d'uso](#esempi-duso)
- [Sviluppo locale](#sviluppo-locale)
- [Contribuire](#contribuire)
- [Licenza](#licenza)

## Panoramica

Visura API fornisce accesso automatizzato ai dati catastali italiani tramite il portale SISTER. Usa l'automazione del browser (Playwright) e offre un'interfaccia REST per:

1. **Estrarre dati immobiliari** (immobili) per specifiche particelle catastali
2. **Recuperare i titolari** (intestati) di specifici immobili
3. **Estrarre le sezioni territoriali** per tutte le province e comuni d'Italia

### Funzionalità principali

- **Flusso a due fasi**: prima estrae gli immobili, poi gli intestati per immobili specifici
- **Filtro automatico**: esclude gli immobili con partita "Soppressa"
- **Gestione sessione**: mantiene la sessione autenticata con il portale SISTER
- **Coda di elaborazione**: gestisce richieste multiple senza sovraccaricare il portale
- **Ri-autenticazione automatica**: alla scadenza della sessione
- **Distribuzione con Docker**: pronto per il deploy containerizzato

## Architettura

```
┌─────────────────┐    ┌─────────────────┐
│  Applicazione   │───▶│    FastAPI      │
│    client       │    │   (main.py)     │
└─────────────────┘    └─────────────────┘
                               │
                               ▼
                       ┌─────────────────┐
                       │   Playwright    │
                       │  (automazione   │
                       │    browser)     │
                       └─────────────────┘
                               │
                               ▼
                       ┌─────────────────┐
                       │   Portale       │
                       │   SISTER        │
                       └─────────────────┘
```

### Componenti principali

| File | Descrizione |
|------|-------------|
| `main.py` | Applicazione FastAPI con endpoint REST e gestione della coda |
| `utils.py` | Funzioni di automazione del browser con Playwright |
| `docker-compose.yaml` | Orchestrazione dei container |

## Compatibilità SPID

> **Importante**: attualmente il login automatizzato è implementato **esclusivamente** per il provider SPID **CIE Sign / Sielte ID**. Se utilizzi un altro provider SPID, dovrai adattare il flusso di autenticazione in `utils.py` (funzione `login()`).

## Installazione

### Prerequisiti

- Python 3.11+
- Docker e Docker Compose (per il deploy)
- Credenziali valide per il portale SISTER (SPID tramite CIE Sign / Sielte ID)

### Avvio rapido con Docker

```bash
git clone https://github.com/menimenocchio/visura-api.git
cd visura-api

# Configura le variabili d'ambiente
cp .env.example .env
# Modifica .env con le tue credenziali

# Avvia il servizio
docker-compose up -d

# Verifica che il servizio sia attivo
curl http://localhost:8000/health
```

### Installazione manuale

```bash
git clone https://github.com/menimenocchio/visura-api.git
cd visura-api

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# Modifica .env con le tue credenziali

uvicorn main:app --host 0.0.0.0 --port 8000
```

## Configurazione

Crea un file `.env` nella cartella del progetto (vedi `.env.example`):

```env
# Credenziali SPID / Agenzia delle Entrate
ADE_USERNAME=il_tuo_codice_fiscale
ADE_PASSWORD=la_tua_password

# Configurazione opzionale
LOG_LEVEL=INFO
CONCURRENT_WORKERS=3
```

## Endpoint API

### Controllo stato

**`GET /health`** — Verifica lo stato del servizio

```json
{
  "status": "healthy",
  "authenticated": true,
  "queue_size": 0
}
```

### Fase 1: Estrazione immobili

**`POST /visura`** — Estrae gli immobili per una particella catastale

```json
// Richiesta
{
  "provincia": "Trieste",
  "comune": "TRIESTE",
  "foglio": "9",
  "particella": "166",
  "sezione": null,
  "tipo_catasto": "F"   // "F" = Fabbricati, "T" = Terreni (opzionale, se omesso esegue entrambi)
}

// Risposta
{
  "request_ids": ["req_F_1693747200000"],
  "tipos_catasto": ["F"],
  "status": "queued",
  "message": "Richieste aggiunte alla coda per TRIESTE F.9 P.166"
}
```

**`GET /visura/{request_id}`** — Recupera i risultati di una richiesta

```json
{
  "request_id": "req_F_1693747200000",
  "tipo_catasto": "F",
  "status": "completed",
  "data": {
    "immobili": [
      {
        "Foglio": "9",
        "Particella": "166",
        "Sub": "1",
        "Categoria": "A/2",
        "Indirizzo": "Via Roma 123",
        "Rendita": "500.00"
      }
    ],
    "intestati": []
  },
  "timestamp": "2026-03-15T10:30:00"
}
```

### Fase 2: Estrazione intestati

**`POST /visura/intestati`** — Estrae gli intestati di un immobile specifico

```json
// Richiesta
{
  "provincia": "Trieste",
  "comune": "TRIESTE",
  "foglio": "9",
  "particella": "166",
  "tipo_catasto": "F",
  "subalterno": "1"   // Obbligatorio per Fabbricati
}

// Risposta
{
  "request_id": "intestati_F_1_1693747300000",
  "tipo_catasto": "F",
  "subalterno": "1",
  "status": "queued",
  "queue_position": 1
}
```

### Sezioni territoriali

**`POST /sezioni/extract`** — Avvia l'estrazione delle sezioni per tutte le province

```json
// Richiesta
{
  "tipo_catasto": "T",
  "max_province": 200
}

// Risposta
{
  "status": "success",
  "message": "Estrazione completata per tipo catasto T",
  "total_extracted": 42000,
  "tipo_catasto": "T",
  "sezioni": [...]
}
```

### Altro

| Endpoint | Metodo | Descrizione |
|----------|--------|-------------|
| `/shutdown` | POST | Shutdown controllato con logout dal portale |

## Esempi d'uso

### Flusso completo con cURL

```bash
# 1. Avvia l'estrazione dei fabbricati per una particella
curl -X POST "http://localhost:8000/visura" \
  -H "Content-Type: application/json" \
  -d '{"provincia": "Trieste", "comune": "TRIESTE", "foglio": "9", "particella": "166", "tipo_catasto": "F"}'

# 2. Controlla i risultati (attendi qualche secondo)
curl "http://localhost:8000/visura/req_F_1693747200000"

# 3. Estrai gli intestati per un immobile specifico
curl -X POST "http://localhost:8000/visura/intestati" \
  -H "Content-Type: application/json" \
  -d '{"provincia": "Trieste", "comune": "TRIESTE", "foglio": "9", "particella": "166", "tipo_catasto": "F", "subalterno": "1"}'
```

### Client Python

```python
import requests
import time

class ClientVisura:
    def __init__(self, url_base="http://localhost:8000"):
        self.url_base = url_base

    def estrai_immobili(self, provincia, comune, foglio, particella, sezione=None, tipo_catasto="F"):
        """Fase 1: Estrae gli immobili di una particella"""
        dati = {
            "provincia": provincia,
            "comune": comune,
            "foglio": foglio,
            "particella": particella,
            "tipo_catasto": tipo_catasto
        }
        if sezione:
            dati["sezione"] = sezione
        risposta = requests.post(f"{self.url_base}/visura", json=dati)
        return risposta.json()

    def attendi_risultati(self, request_id, attesa_max=300):
        """Attende il completamento di una richiesta"""
        inizio = time.time()
        while time.time() - inizio < attesa_max:
            risposta = requests.get(f"{self.url_base}/visura/{request_id}")
            risultato = risposta.json()
            if risultato["status"] in ["completed", "error"]:
                return risultato
            time.sleep(5)
        raise TimeoutError(f"Richiesta {request_id} non completata entro {attesa_max} secondi")

    def estrai_intestati(self, provincia, comune, foglio, particella, tipo_catasto, subalterno=None, sezione=None):
        """Fase 2: Estrae gli intestati di un immobile"""
        dati = {
            "provincia": provincia,
            "comune": comune,
            "foglio": foglio,
            "particella": particella,
            "tipo_catasto": tipo_catasto
        }
        if subalterno:
            dati["subalterno"] = subalterno
        if sezione:
            dati["sezione"] = sezione
        risposta = requests.post(f"{self.url_base}/visura/intestati", json=dati)
        return risposta.json()

# Esempio d'uso
client = ClientVisura()

# Estrai i fabbricati
estrazione = client.estrai_immobili("Trieste", "TRIESTE", "9", "166", tipo_catasto="F")
request_id = estrazione["request_ids"][0]

# Attendi i risultati
risultati = client.attendi_risultati(request_id)
print(f"Trovati {len(risultati['data']['immobili'])} immobili")

# Estrai gli intestati del primo immobile
intestati = client.estrai_intestati("Trieste", "TRIESTE", "9", "166", "F", subalterno="1")
```

## Sviluppo locale

### Configurazione

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-cov black ruff   # dipendenze di sviluppo
playwright install chromium
```

### Eseguire i test

```bash
python -m pytest test_*.py -v
```

### Formattazione e linting

```bash
black .          # formattazione automatica
ruff check .     # controllo linting
```

### Docker

```bash
# Avvia il servizio
docker-compose up --build

# Visualizza i log
docker-compose logs -f visure-service
```

### Log

I log vengono scritti sia su console che su file (`logs/visura.log`).

Livelli disponibili: `DEBUG`, `INFO`, `WARNING`, `ERROR`

```bash
# Abilita i log dettagliati
export LOG_LEVEL=DEBUG
```

## Considerazioni sulle prestazioni

- Il portale SISTER ha limiti interni di frequenza delle richieste
- Il servizio accoda le richieste per non sovraccaricare il portale
- Tempo tipico di elaborazione: 30-60 secondi per richiesta
- **Requisiti minimi**: 2 GB RAM, 2 core CPU, connessione stabile

## Risoluzione dei problemi

| Problema | Soluzione |
|----------|----------|
| Il servizio non si avvia | Controlla i log: `docker-compose logs visure-service` |
| Autenticazione fallita | Verifica le credenziali nel file `.env` |
| Risposte lente | Controlla la dimensione della coda: `GET /health` |
| Sessione scaduta | Il servizio si ri-autentica automaticamente |

## Contribuire

Leggi [CONTRIBUTING.md](CONTRIBUTING.md) per le linee guida su come contribuire al progetto.

## Licenza

Questo progetto è distribuito sotto la licenza **GNU General Public License v3.0**. Vedi il file [LICENSE](LICENSE) per i dettagli.

---

*Ultimo aggiornamento: marzo 2026*
