#!/usr/bin/env bash
set -euo pipefail

# CLI exports for the query payload fixtures in tests/fixtures.py and
# tests/test_fixtures.py.
#
# The default DRY_RUN=1 makes these commands validate the CLI payload mapping
# without submitting requests to SISTER. To execute live queries:
#   DRY_RUN=0 WAIT=1 sister dev fixture-payloads
#
# Paid ipotecaria commands are kept in dry-run mode unless PAID=1 is set. Even
# then the CLI sends auto_confirm=false; it does not authorize a payment.

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DRY_RUN="${DRY_RUN:-1}"
WAIT="${WAIT:-0}"
PAID="${PAID:-0}"
SISTER_CLI="${SISTER_CLI:-.venv/bin/sister}"

common_flags=()
if [[ "$DRY_RUN" == "1" ]]; then
  common_flags+=(--dry-run)
fi
if [[ "$WAIT" == "1" ]]; then
  common_flags+=(--wait)
fi

paid_flags=()
if [[ "$PAID" != "1" ]]; then
  paid_flags+=(--dry-run)
fi
if [[ "$WAIT" == "1" ]]; then
  paid_flags+=(--wait)
fi

workflow_flags=()
if [[ "$DRY_RUN" == "1" ]]; then
  workflow_flags+=(--dry-run)
fi

# Base visura and intestati payloads from tests/test_fixtures.py
"$SISTER_CLI" query search --provincia Trieste --comune TRIESTE --foglio 9 --particella 166 --tipo-catasto F "${common_flags[@]}"
"$SISTER_CLI" query intestati --provincia Trieste --comune TRIESTE --foglio 9 --particella 166 --tipo-catasto F --subalterno 3 "${common_flags[@]}"
"$SISTER_CLI" query search --provincia Roma --comune ROMA --foglio 100 --particella 50 --tipo-catasto F "${common_flags[@]}"
"$SISTER_CLI" query intestati --provincia Roma --comune ROMA --foglio 100 --particella 50 --tipo-catasto F --subalterno 3 "${common_flags[@]}"

# Visura soggetto payloads
"$SISTER_CLI" query soggetto --cf RSSMRI85E28H501E "${common_flags[@]}"
"$SISTER_CLI" query soggetto --cf BNCLRA90A41H501Z --tipo-catasto F --provincia MI "${common_flags[@]}"
"$SISTER_CLI" query soggetto --cf RSSMRI85E28H501E --tipo-catasto T "${common_flags[@]}"

# Persona giuridica payloads
"$SISTER_CLI" query azienda --id 02471840997 "${common_flags[@]}"
"$SISTER_CLI" query azienda --id 03618371201 --tipo-catasto F --provincia BO "${common_flags[@]}"
"$SISTER_CLI" query azienda --id "TIGULLIO IMMOBILIARE SRL" --tipo-catasto E "${common_flags[@]}"

# Elenco immobili payloads
"$SISTER_CLI" query elenco --provincia Trieste --comune TRIESTE --tipo-catasto T "${common_flags[@]}"
"$SISTER_CLI" query elenco --provincia Trieste --comune TRIESTE --tipo-catasto F --foglio 9 "${common_flags[@]}"
"$SISTER_CLI" query elenco --provincia Roma --comune ROMA "${common_flags[@]}"

# Generic SISTER payloads
"$SISTER_CLI" query indirizzo --provincia Terni --comune TERNI --tipo-catasto F --indirizzo "VIA DEL RIVO" "${common_flags[@]}"
"$SISTER_CLI" query partita --provincia Roma --comune ROMA --tipo-catasto T --partita 123456 "${common_flags[@]}"
"$SISTER_CLI" query nota --provincia Roma --tipo-catasto T --numero 12345 --anno-nota 2020 "${common_flags[@]}"
"$SISTER_CLI" query mappa --provincia Trieste --comune TRIESTE --tipo-catasto T --foglio 9 "${common_flags[@]}"
"$SISTER_CLI" query export-mappa --provincia Trieste --comune TRIESTE --tipo-catasto T --foglio 9 "${common_flags[@]}"
"$SISTER_CLI" query originali --provincia Trieste --comune TRIESTE --tipo-catasto T "${common_flags[@]}"
"$SISTER_CLI" query fiduciali --provincia Roma --comune ROMA --tipo-catasto T "${common_flags[@]}"
"$SISTER_CLI" query ispezioni --provincia Trieste --comune TRIESTE --tipo-catasto T --foglio 9 "${common_flags[@]}"
"$SISTER_CLI" query ispezioni-cartacee --provincia Trieste --comune TRIESTE --tipo-catasto T "${common_flags[@]}"
"$SISTER_CLI" query elaborato-planimetrico --provincia Roma --comune ROMA "${common_flags[@]}"

# Ispezione ipotecaria payloads. These are paid-service commands.
"$SISTER_CLI" query ipotecaria-immobile --provincia Roma --comune ROMA --foglio 100 --particella 50 --tipo-catasto F "${paid_flags[@]}"
"$SISTER_CLI" query ipotecaria-azienda --provincia Roma --id 02471840997 "${paid_flags[@]}"
"$SISTER_CLI" query ipotecaria-nota --provincia Roma --numero 12345 --anno 2020 "${paid_flags[@]}"

# Persona fisica starts the free preview; any paid action remains in the admin flow.
"$SISTER_CLI" query ipotecaria-persona-fisica --provincia Roma --cf RSSMRI85E28H501E "${common_flags[@]}"

# Workflow payloads
"$SISTER_CLI" workflow run --preset due-diligence --provincia Roma --comune ROMA --foglio 100 --particella 50 "${workflow_flags[@]}"
"$SISTER_CLI" workflow run --preset patrimonio --cf RSSMRI85E28H501E "${workflow_flags[@]}"
"$SISTER_CLI" workflow run --preset fondiario --provincia Trieste --comune TRIESTE --foglio 9 "${workflow_flags[@]}"
"$SISTER_CLI" workflow run --preset aziendale --azienda 02471840997 "${workflow_flags[@]}"
"$SISTER_CLI" workflow run --preset storico --provincia Trieste --comune TRIESTE --foglio 9 --particella 166 "${workflow_flags[@]}"
"$SISTER_CLI" workflow run --preset indirizzo --provincia Terni --comune TERNI --indirizzo "VIA DEL RIVO 1" "${workflow_flags[@]}"
"$SISTER_CLI" workflow run --preset cross-reference --cf RSSMRI85E28H501E --azienda 02471840997 "${workflow_flags[@]}"
"$SISTER_CLI" workflow run --preset full-due-diligence --provincia Roma --comune ROMA --foglio 100 --particella 50 "${workflow_flags[@]}"
"$SISTER_CLI" workflow run --preset full-patrimonio --cf RSSMRI85E28H501E "${workflow_flags[@]}"
"$SISTER_CLI" workflow run --preset full-aziendale --azienda 02471840997 "${workflow_flags[@]}"
