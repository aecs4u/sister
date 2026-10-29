#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_LIST_PATH="$ROOT/inputs/pvp_sales/IS.md"

if [[ $# -gt 2 ]]; then
  printf 'Usage: %s [SOURCE_PATH [BATCH_DIR]]\n' "$0" >&2
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
