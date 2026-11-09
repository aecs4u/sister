#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -n "${PYTHON_RUNNER:-}" ]]; then
  read -r -a PYTHON_CMD <<< "$PYTHON_RUNNER"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_CMD=("$ROOT/.venv/bin/python")
elif command -v uv >/dev/null 2>&1; then
  PYTHON_CMD=(uv run --no-project python)
else
  PYTHON_CMD=()
fi

usage() {
  cat <<EOF
Usage: $0 [OPTIONS] SOURCE_LIST.md [BATCH_DIR]

Build a SISTER batch from a Markdown list and run it in the current logged-in SISTER session.
Start it right after "uv run sister app login": the session lasts about 30 minutes from the login.

Arguments:
  SOURCE_LIST.md   Markdown list with <!-- sister-batch-table {...} --> tables or the older codici lists
                   (format: docs/batch_markdown_format.md). Each table header sets the query and its parameters,
                   e.g. {"command": "azienda", "params": {"tipo_catasto": "E", "con_intestati": true}}.
  BATCH_DIR        Folder of the batch (batch.json, batch_journal.jsonl, run.log, service.log, report.json, one
                   batch_NNNN_<command>.json per row). Default: outputs/batches/<list name>_<date>_<time>.

Options (the default is shown in brackets):
  --preflight-cf CF       Prove the session with one real subject search of this codice fiscale before the batch.
                          Use a code that SISTER finds: "nessuna corrispondenza" counts as a failed proof.
                          [none; environment SISTER_PREFLIGHT_CF]
  --email-progress        Send a final progress summary through aecs4u-email when the session run closes.
                          [off; recipient demo@aecs4u.com]
  --email-to ADDRESS      Recipient for --email-progress. [demo@aecs4u.com]
  --minutes N             Minutes after the login at which no new row starts (SISTER itself ends the session after
                          about 30). [26]
  --logged-in-at HH:MM    Time of the login the minutes are counted from. [the time this command is started]
  --grace-seconds N       After the stop time a row still running this many seconds later is interrupted
                          (the journal keeps it; --resume continues). [60]
  --port N                Port of the service this run starts; it must be free. [8027]
  --cdp URL               Chrome with the SISTER login (remote debugging address). [environment BROWSER_CDP_ENDPOINT, else http://localhost:9222]
  --tipo-richiesta T      "Tipo richiesta" of the person searches when the list does not say: attualita | storica.
                          [attualita, the portal's own default]
  --keep-answered         Do not drop the rows that already have an answer. Rows asking for storica are always kept.
                          [off: answered rows are dropped]
  --dry-run               Preview the source plan and pruning in a temporary folder; do not change BATCH_DIR or start the service. [off]
  --probe                 Also open a scratch tab to test the login first. Not advised: a second tab on a live
                          session made SISTER answer lock.html. [off]
  -h, --help              Show this help and exit.

Session duration:
  SISTER ends every session about 30 minutes after the login, whatever the activity (measured 29.6 to 30.4 min);
  nothing in this tool can extend it. --minutes [26] is the time after which no new row starts, so with the
  default the batch stops cleanly before the portal ends the session (a row still running 60 s later is interrupted
  and repeated by --resume). A larger value runs until the first "sessione scaduta" answer (the row in flight then
  fails and is repeated by --resume). Then log in again (uv run sister app login) and run the same command.

What it does, in order:
  1. Without BATCH_DIR, resumes the latest saved batch of this list. A batch that is already finished is not
     resumed: a new folder is started (rows with an answer are dropped again below).
  2. Rebuilds batch.json from the list (batch build-list).
  3. Takes the time of this command (or --logged-in-at) as the login time and sets the stop --minutes after it.
     With the default 26 minutes, the run stops before the portal ends the session; a larger value can let SISTER end it.
  4. Drops the rows that already have an answer in the database and the codici fiscali with a wrong check
     character (the plan is copied to BATCH_DIR/backups/ first), then attaches a service on port 8027 to the
     SISTER session in the shared Chrome (BROWSER_CDP_ENDPOINT, default http://localhost:9222). It never logs in.
  5. Runs the rows one after the other (query batch --resume): a row already submitted is polled again, never sent
     twice; SISTER's "nessuna corrispondenza" and "criteri da affinare" answers are final and not repeated.
     It stops by itself at the first expired-session answer.
  6. Imports the ready, not-yet-stored documents from SISTER's Richieste page, then stops its service. Chrome and
     the SISTER session are left as they are.

Files and progress:
  BATCH_DIR/run.log shows the rows; a progress bar shows the rows done and the session time left. From another
  terminal: uv run sister batch progress BATCH_DIR. Resume after a new login by running the same command again.

Exit status:
  0 finished, or stopped at the deadline/session end (run the same command again after a new login)
  2 wrong usage, no usable SISTER session in Chrome, or the session is almost over (log in again)
  3 the --preflight-cf search failed: the session is not usable

Environment (optional):
  PYTHON_RUNNER                     Python command to use, e.g. "uv run --no-project python".
  SISTER_PREFLIGHT_CF               Default for --preflight-cf.
  SISTER_OMONIMI_MAX_SECONDS        Persona giuridica: seconds before stopping to open more registry records of one
                                    code (default 150; the rest is continued by the next run).
  SISTER_INTESTATI_MAX_SECONDS      Rows with con_intestati: seconds to read the owners of the properties (default 420).
  SISTER_SESSION_RECHECK_SECONDS    Trust a good session check this long (set 1500 for batch runs).
  Other settings: .env and docs/portal_session.md.

Examples:
  $0 inputs/codici_fiscali.md
  $0 inputs/grandi_proprietari.md --preflight-cf <CODICE_FISCALE_TROVATO>
  $0 inputs/pvp_sales/IS.md outputs/batches/IS_run_01
  $0 inputs/codici_fiscali.md --minutes 28 --logged-in-at 09:12
  $0 inputs/codici_fiscali.md --dry-run
EOF
}

POSITIONAL=()
PREFLIGHT_CF=""
LOGIN_AT=""
TIPO_RICHIESTA=""
DRY_RUN=false
PASS_ARGS=()   # forwarded as they are to "batch run-session"
need_value() {
  if (($# < 2)) || [[ -z "$2" ]]; then
    printf '%s requires a value.\n' "$1" >&2
    usage >&2
    exit 2
  fi
}
while (($#)); do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --preflight-cf|--logged-in-at|--tipo-richiesta|--minutes|--grace-seconds|--port|--cdp|--email-to)
      need_value "$@"
      case "$1" in
        --preflight-cf) PREFLIGHT_CF="$2" ;;
        --logged-in-at) LOGIN_AT="$2" ;;
        --tipo-richiesta) TIPO_RICHIESTA="$2" ;;
        *) PASS_ARGS+=("$1" "$2") ;;
      esac
      shift 2
      ;;
    --preflight-cf=*|--logged-in-at=*|--tipo-richiesta=*|--minutes=*|--grace-seconds=*|--port=*|--cdp=*|--email-to=*)
      need_value "${1%%=*}" "${1#*=}"
      case "${1%%=*}" in
        --preflight-cf) PREFLIGHT_CF="${1#*=}" ;;
        --logged-in-at) LOGIN_AT="${1#*=}" ;;
        --tipo-richiesta) TIPO_RICHIESTA="${1#*=}" ;;
        *) PASS_ARGS+=("${1%%=*}" "${1#*=}") ;;
      esac
      shift
      ;;
    --dry-run|--keep-answered|--probe|--email-progress)
      PASS_ARGS+=("$1")
      if [[ "$1" == "--dry-run" ]]; then
        DRY_RUN=true
      fi
      shift
      ;;
    --)
      shift
      POSITIONAL+=("$@")
      break
      ;;
    -* )
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

if [[ ${#POSITIONAL[@]} -lt 1 || ${#POSITIONAL[@]} -gt 2 ]]; then
  usage >&2
  exit 2
fi

SOURCE_LIST="$(realpath -- "${POSITIONAL[0]}")"
if [[ ! -f "$SOURCE_LIST" ]]; then
  printf 'Source list is not a file: %s\n' "$SOURCE_LIST" >&2
  exit 2
fi
if [[ ${#PYTHON_CMD[@]} -eq 0 ]] || { ! command -v "${PYTHON_CMD[0]}" >/dev/null 2>&1 && [[ ! -x "${PYTHON_CMD[0]}" ]]; }; then
  printf 'Python runner unavailable. Install the project venv or set PYTHON_RUNNER (for example: "uv run --no-project python").\n' >&2
  exit 2
fi

# Capture this before batch generation, as the script is meant to be started just after login.
LOGIN_TIME="${LOGIN_AT:-$(date +%H:%M)}"
if [[ ${#POSITIONAL[@]} -eq 2 ]]; then
  BATCH_DIR="$(realpath -m -- "${POSITIONAL[1]}")"
else
  LIST_NAME="$(basename -- "$SOURCE_LIST" .md)"
  BATCH_ROOT="$ROOT/outputs/batches"
  BATCH_DIR=""
  # Timestamped folder names sort chronologically, so the last matching journal is the latest run.
  for candidate in "$BATCH_ROOT/${LIST_NAME}_"*; do
    if [[ -d "$candidate" && -f "$candidate/batch_journal.jsonl" ]]; then
      BATCH_DIR="$candidate"
    fi
  done
  # A finished batch is not resumed: its journal says every row is done, so nothing (not even a `reuse: force` row)
  # would be sent again. Start a new folder; the pruning still drops the rows already answered.
  if [[ -n "$BATCH_DIR" ]] && "${PYTHON_CMD[@]}" -c 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from sister.command_tools.batch_progress import is_finished; sys.exit(0 if is_finished(Path(sys.argv[2])) else 1)' "$ROOT" "$BATCH_DIR" 2>/dev/null; then
    printf 'Saved batch for %s is finished (%s): starting a new one\n' "$(basename -- "$SOURCE_LIST")" "$BATCH_DIR"
    BATCH_DIR=""
  fi
  if [[ -n "$BATCH_DIR" ]]; then
    printf 'Resuming saved batch for %s: %s\n' "$(basename -- "$SOURCE_LIST")" "$BATCH_DIR"
  else
    RUN_ID="$(date +%Y%m%d_%H%M%S)"
    BATCH_DIR="$BATCH_ROOT/${LIST_NAME}_${RUN_ID}"
  fi
fi

if [[ "$DRY_RUN" == true ]]; then
  TEMP_BATCH_DIR="$(mktemp -d)"
  BATCH_DIR="$TEMP_BATCH_DIR"
  trap 'rm -rf -- "$TEMP_BATCH_DIR"' EXIT
fi

mkdir -p -- "$BATCH_DIR"
BUILD_ARGS=()
if [[ -n "$TIPO_RICHIESTA" ]]; then
  BUILD_ARGS+=(--tipo-richiesta "$TIPO_RICHIESTA")
fi
"${PYTHON_CMD[@]}" -m sister.cli batch build-list "$SOURCE_LIST" --output-dir "$BATCH_DIR" "${BUILD_ARGS[@]}"
RUN_ARGS=(--batch "$BATCH_DIR" --logged-in-at "$LOGIN_TIME")
if [[ -n "$PREFLIGHT_CF" ]]; then
  RUN_ARGS+=(--preflight-cf "$PREFLIGHT_CF")
fi
if [[ "$DRY_RUN" == true ]]; then
  if "${PYTHON_CMD[@]}" -m sister.cli batch run-session "${RUN_ARGS[@]}" "${PASS_ARGS[@]}"; then
    exit 0
  else
    RUN_STATUS=$?
    exit "$RUN_STATUS"
  fi
fi
exec "${PYTHON_CMD[@]}" -m sister.cli batch run-session "${RUN_ARGS[@]}" "${PASS_ARGS[@]}"
