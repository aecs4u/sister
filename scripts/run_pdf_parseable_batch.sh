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
  printf 'Python runner unavailable. Install the project venv or set PYTHON_RUNNER.\n' >&2
  exit 2
fi

cd "$ROOT"
exec "${PYTHON_CMD[@]}" -m sister.cli batch run-pdf-parseable "$@"
