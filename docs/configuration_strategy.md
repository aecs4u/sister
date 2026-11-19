# Configurazione per ambiente

SISTER usa un profilo di configurazione per ogni processo. I valori condivisi
con gli altri servizi vivono nei file `.env` della workspace; il file
`sister/.env` contiene i valori specifici dell'applicazione e non viene
condiviso o versionato.

## Profili

| Profilo | Uso | Log | Database |
| --- | --- | --- | --- |
| `development` (`dev`) | Lavoro locale | `DEBUG` | `SISTER_DATABASE_DSN_LOCALHOST` |
| `testing` (`test`) | Test automatizzati | `DEBUG` | `SISTER_DATABASE_DSN_TEST` |
| `staging` | Verifica release | `INFO` | `SISTER_DATABASE_DSN_NEON` |
| `production` (`prod`) | Servizio live | `INFO` | `SISTER_DATABASE_DSN_NEON` |

Staging usa intenzionalmente lo stesso database di produzione. Il selettore non
prova un secondo DSN se quello del profilo scelto manca. I test creano uno
schema temporaneo sul database di test e accettano solo connessioni PostgreSQL
locali.

## Selezione e caricamento

`scripts/start.sh` accetta `--env dev|test|staging|production` e usa `dev` se
l'opzione manca. Il processo esporta il valore canonico in `ENVIRONMENT` prima
di avviare Uvicorn. Anche `ENVIRONMENT` impostato direttamente nel processo
seleziona il profilo per gli altri entry point.

Il loader Python legge i file dalla root del repository e dalla cartella SISTER,
indipendentemente dalla directory corrente, in questo ordine:

1. `../.env` (valori condivisi di base)
2. `.env` di SISTER (valori SISTER e segreti locali)
3. `../.env.<profilo>` (impostazioni condivise del profilo)
4. `.env.<profilo>` di SISTER (eventuali override locali del profilo)
5. variabili già presenti nel processo (priorità massima)

Il profilo è scelto da `ENVIRONMENT` nel processo, poi dal `.env` SISTER, poi
dal `.env` condiviso; se non è impostato, il valore predefinito è `development`.
I nomi `dev`, `local` e `localhost` sono alias di `development`; `test` è alias
di `testing`; `stage` è alias di `staging`; `prod` è alias di `production`.
Un nome sconosciuto genera un errore.

I profili condivisi definiscono `ENVIRONMENT`, `DEBUG`, `LOG_LEVEL` e
`LOGFIRE_ENVIRONMENT`. Se i file di profilo condivisi non sono disponibili, il
loader usa il livello `DEBUG` per development/testing e `INFO` per
staging/production. Non copiare i valori condivisi in `sister/.env` o nei
workflow. `.env.example` documenta le impostazioni specifiche di SISTER;
`.env` e `.env.<profilo>` locali restano esclusi da Git.

## Database

SISTER seleziona un solo DSN in base al profilo attivo:

| Profilo | Variabile richiesta |
| --- | --- |
| `development` | `SISTER_DATABASE_DSN_LOCALHOST` |
| `testing` | `SISTER_DATABASE_DSN_TEST` |
| `staging`, `production` | `SISTER_DATABASE_DSN_NEON` |

I tre valori possono essere definiti nel `.env` locale. In Cloud Run il workflow
imposta `ENVIRONMENT=production` e inietta `SISTER_DATABASE_DSN_NEON` da Secret
Manager. Non inserire credenziali reali in `.env.example` o nei file versionati.
