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
  printf 'Usage: %s [OPTIONS] SOURCE_LIST.md [BATCH_DIR]\n' "$0"
  printf '\nBuild a SISTER batch from a supported Markdown list and run it in the current logged-in session.\n'
  printf 'Without BATCH_DIR, resumes the latest saved batch for this list when one exists.\n'
  printf 'After the batch, imports ready, not-yet-stored documents from Richieste.\n'
  printf '\nOptions:\n  --preflight-cf CF  Run one real subject search to verify the session before the batch.\n'
  printf '  -h, --help        Show this help and exit.\n'
  printf '\nExamples:\n  %s inputs/codici_fiscali.all.md\n  %s inputs/pvp_sales/IS.md outputs/batches/IS_run_01\n' "$0" "$0"
}

POSITIONAL=()
PREFLIGHT_CF=""
while (($#)); do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --preflight-cf)
      if (($# < 2)) || [[ -z "$2" ]]; then
        printf '%s\n' '--preflight-cf requires a codice fiscale.' >&2
        usage >&2
        exit 2
      fi
      PREFLIGHT_CF="$2"
      shift 2
      ;;
    --preflight-cf=*)
      PREFLIGHT_CF="${1#*=}"
      if [[ -z "$PREFLIGHT_CF" ]]; then
        printf '%s\n' '--preflight-cf requires a codice fiscale.' >&2
        usage >&2
        exit 2
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
LOGIN_TIME="$(date +%H:%M)"
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

mkdir -p -- "$BATCH_DIR"
"${PYTHON_CMD[@]}" -m sister.cli batch build-list "$SOURCE_LIST" --output-dir "$BATCH_DIR"
RUN_ARGS=(--batch "$BATCH_DIR" --logged-in-at "$LOGIN_TIME")
if [[ -n "$PREFLIGHT_CF" ]]; then
  RUN_ARGS+=(--preflight-cf "$PREFLIGHT_CF")
fi
exec "${PYTHON_CMD[@]}" -m sister.cli batch run-session "${RUN_ARGS[@]}"
