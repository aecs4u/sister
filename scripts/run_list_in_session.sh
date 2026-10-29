#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -n "${PYTHON_RUNNER:-}" ]]; then
  read -r -a PYTHON_CMD <<< "$PYTHON_RUNNER"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_CMD=("$ROOT/.venv/bin/python")
elif command -v uv >/dev/null 2>&1; then
  PYTHON_CMD=(uv run --no-project)
else
  PYTHON_CMD=()
fi

usage() {
  printf 'Usage: %s SOURCE_LIST.md [BATCH_DIR]\n' "$0" >&2
  printf 'Build a SISTER batch from the Markdown list and run it in the current logged-in session.\n' >&2
  printf 'Without BATCH_DIR, resumes the latest saved batch for this list when one exists.\n' >&2
  printf 'After the batch, imports ready, not-yet-stored documents from Richieste.\n' >&2
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
  exit 2
fi

SOURCE_LIST="$(realpath -- "$1")"
if [[ ! -f "$SOURCE_LIST" ]]; then
  printf 'Source list is not a file: %s\n' "$SOURCE_LIST" >&2
  exit 2
fi
if [[ ${#PYTHON_CMD[@]} -eq 0 ]] || { ! command -v "${PYTHON_CMD[0]}" >/dev/null 2>&1 && [[ ! -x "${PYTHON_CMD[0]}" ]]; }; then
  printf 'Python runner unavailable. Install the project venv or set PYTHON_RUNNER (for example: "uv run --no-project").\n' >&2
  exit 2
fi

# Capture this before batch generation, as the script is meant to be started just after login.
LOGIN_TIME="$(date +%H:%M)"
if [[ $# -eq 2 ]]; then
  BATCH_DIR="$(realpath -m -- "$2")"
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
  if [[ -n "$BATCH_DIR" ]]; then
    printf 'Resuming saved batch for %s: %s\n' "$(basename -- "$SOURCE_LIST")" "$BATCH_DIR"
  else
    RUN_ID="$(date +%Y%m%d_%H%M%S)"
    BATCH_DIR="$BATCH_ROOT/${LIST_NAME}_${RUN_ID}"
  fi
fi

mkdir -p -- "$BATCH_DIR"
"${PYTHON_CMD[@]}" "$ROOT/scripts/build_batch_from_list.py" "$SOURCE_LIST" --output-dir "$BATCH_DIR"
exec "${PYTHON_CMD[@]}" "$ROOT/scripts/run_in_session.py" --batch "$BATCH_DIR" --logged-in-at "$LOGIN_TIME"
