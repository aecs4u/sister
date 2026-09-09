#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

START_APP_NAME="SISTER"
START_APP_MODULE="sister.main:app"
START_DEFAULT_PORT=8025
START_DEFAULT_HOST="0.0.0.0"
START_WORKERS=1
START_UVICORN_ARGS=(
    --reload-include "*.py"
    --reload-exclude "templates/*"
    --reload-exclude "static/*"
)
START_PROJECT_ROOT="${PROJECT_ROOT}"
START_SKIP_SYNC="${START_SKIP_SYNC:-true}"

FORWARD_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-query)
            export SISTER_NO_QUERY=1
            shift
            ;;
        *)
            FORWARD_ARGS+=("$1")
            shift
            ;;
    esac
done

SHARED_START="${PROJECT_ROOT}/../bin/start-common.sh"
source "${SHARED_START}"
start_common_main "${FORWARD_ARGS[@]}"
