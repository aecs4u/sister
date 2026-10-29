#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_LIST_PATH="$ROOT/inputs/pvp_sales/IS.md"

usage() {
  printf 'Usage: %s [OPTIONS] [SOURCE_PATH [BATCH_DIR]]\n' "$0"
  printf '\nPrompt for a Markdown list path (default: %s), then run it in the current SISTER session.\n' "$DEFAULT_LIST_PATH"
  printf 'The optional BATCH_DIR is passed to scripts/run_list_in_session.sh.\n'
  printf '\nOptions:\n  -h, --help  Show this help and exit.\n'
  printf '\nExamples:\n  %s\n  %s inputs/pvp_sales/IS.md outputs/batches/IS_run_01\n' "$0" "$0"
}

for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
  esac
done

if [[ $# -gt 2 ]]; then
  usage >&2
  exit 2
fi

if [[ $# -gt 0 ]]; then
  SOURCE_LIST="$1"
  shift
else
  SOURCE_LIST=""
  read -r -p "Markdown list path to process [$DEFAULT_LIST_PATH]: " SOURCE_LIST || true
  SOURCE_LIST="${SOURCE_LIST:-$DEFAULT_LIST_PATH}"
fi

if [[ "$SOURCE_LIST" != /* ]]; then
  SOURCE_LIST="$ROOT/$SOURCE_LIST"
fi

if [[ ! -f "$SOURCE_LIST" ]]; then
  printf 'Source list not found: %s\n' "$SOURCE_LIST" >&2
  exit 2
fi

exec "$ROOT/scripts/run_list_in_session.sh" "$SOURCE_LIST" "$@"
