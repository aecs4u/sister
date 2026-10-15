# MCP locale per SISTER

Il progetto include un server [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) locale. Un client LLM
compatibile può consultare i tipi di query e lo storico, controllare lo stato del servizio e preparare o inviare una
query usando la stessa definizione dei parametri della CLI.

## Prerequisiti e avvio

Il server MCP è un client dell'API SISTER: non avvia il browser e non esegue il login al portale. Avvia prima il servizio
SISTER e assicurati che la sessione del portale sia attiva. L'API predefinita è `http://localhost:8025`.

Installa la dipendenza facoltativa e avvia il server:

```bash
uv sync --extra mcp
uv run sister-mcp
```

Il trasporto è **stdio**: normalmente è il client LLM ad avviare e chiudere il processo. Configura il client con un
comando equivalente al seguente, sostituendo il percorso con quello assoluto del clone:

```toml
[mcp_servers.sister]
command = "uv"
args = ["run", "--extra", "mcp", "sister-mcp"]
cwd = "/percorso/assoluto/sister"
```

Il formato esatto dipende dal client; conserva comando, argomenti e directory di lavoro. L'URL si configura con
`VISURA_API_URL`; la chiave API con `VISURA_API_KEY` (oppure `API_KEY`, lo stesso nome usato dal servizio). Se non
imposti l'URL, viene usato `http://localhost:8025`. Il processo carica le variabili dal `.env` del progetto. Non inserire
chiavi API nella configurazione condivisa o nel repository.

## Strumenti disponibili

| Strumento | Funzione |
|---|---|
| `list_queries` | Elenca i comandi supportati, i parametri, le scelte e i requisiti. |
| `preview_query` | Valida i parametri senza contattare l'API e crea un'anteprima valida 15 minuti. |
| `submit_query` | Invia una query preparata dopo conferma esplicita; l'anteprima è monouso. |
| `get_request` | Legge stato e risultato di una richiesta tramite ID. |
| `search_history` | Cerca le risposte conservate applicando filtri catastali. |
| `sister_health` | Controlla la raggiungibilità e lo stato dell'API. |

### Flusso di invio

1. Chiama `list_queries` per trovare il comando e i nomi esatti dei parametri.
2. Chiama `preview_query`. La validazione è locale e non invia richieste al portale.
3. Mostra all'utente comando e parametri dell'anteprima. Chiama `submit_query` con il relativo `preview_id` e
   `confirm_submission=true` solo dopo che l'utente ha chiesto esplicitamente di inviare quella query.
4. Per impostazione predefinita, una risposta precedente identica viene riusata se disponibile; la risposta può essere
   marcata `stale`. `force=true` ignora il riuso e può avviare una nuova operazione a pagamento: usalo solo su richiesta
   esplicita dell'utente.
5. Se la risposta contiene ID di richiesta, usa `get_request` per consultarne stato e dati. Se il portale richiede un
   CAPTCHA, una persona deve completarlo nella sessione browser SISTER.

La conferma è un requisito del flusso dello strumento. Le annotazioni MCP possono aiutare il client a mostrare una
richiesta di approvazione, ma il comportamento dipende dal client: abilita le conferme per le azioni con effetti esterni.

## Dati e compatibilità

Risultati e storico possono contenere dati personali e patrimoniali. Collega questo server solo a client e modelli
autorizzati a riceverli. L'invio di query contatta il portale SISTER e può comportare costi.

Questo server è locale e usa stdio. **ChatGPT web non avvia direttamente server stdio locali**: per collegarlo a ChatGPT
serve un endpoint MCP remoto HTTPS oppure, nei prodotti supportati, un [Secure MCP Tunnel](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt).
Questa implementazione non espone un endpoint remoto.
