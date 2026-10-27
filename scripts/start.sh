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
    --reload-exclude '.venv'
    --reload-exclude '*.pyc'
    --reload-exclude '__pycache__'
    --reload-exclude 'logs/*'
    --reload-exclude 'logs'
    --reload-exclude 'outputs'
    --reload-exclude '*.log'
    --reload-exclude 'outputs/*'
    --reload-include "*.py"
    --reload-include "*.html"
    --reload-exclude "static/*"
)
START_DEFAULT_RELOAD=true
START_SKIP_SYNC="${START_SKIP_SYNC:-true}"
START_PROJECT_ROOT="${PROJECT_ROOT}"

SHARED_START="${AECS4U_START_COMMON:-${SCRIPT_DIR}/start-common.sh}"
if [[ ! -r "${SHARED_START}" ]]; then
    SHARED_START="${PROJECT_ROOT}/../bin/start-common.sh"
fi
SHARED_START="${PROJECT_ROOT}/../bin/start-common.sh"
source "${SHARED_START}"

# Uvicorn runs the application lifespan before the final bind. Catch an
# already-running local instance first so a duplicate launch does not perform
# database/browser startup and then fail with EADDRINUSE.
START_CHECK_HOST="${START_HOST:-${HOST:-${START_DEFAULT_HOST}}}"
START_CHECK_PORT="${START_PORT:-${PORT:-${START_DEFAULT_PORT}}}"
START_CHECK_HELP=false
START_CHECK_ARGUMENTS_VALID=true
START_CHECK_ARGS=("$@")
for ((START_CHECK_INDEX = 0; START_CHECK_INDEX < ${#START_CHECK_ARGS[@]}; START_CHECK_INDEX++)); do
    case "${START_CHECK_ARGS[START_CHECK_INDEX]}" in
        -h|--help)
            START_CHECK_HELP=true
            break
            ;;
        --)
            break
            ;;
        -H|--host)
            if (( START_CHECK_INDEX + 1 < ${#START_CHECK_ARGS[@]} )); then
                START_CHECK_INDEX=$((START_CHECK_INDEX + 1))
                START_CHECK_HOST="${START_CHECK_ARGS[START_CHECK_INDEX]}"
            else
                START_CHECK_ARGUMENTS_VALID=false
            fi
            ;;
        -p|--port)
            if (( START_CHECK_INDEX + 1 < ${#START_CHECK_ARGS[@]} )); then
                START_CHECK_INDEX=$((START_CHECK_INDEX + 1))
                START_CHECK_PORT="${START_CHECK_ARGS[START_CHECK_INDEX]}"
            else
                START_CHECK_ARGUMENTS_VALID=false
            fi
            ;;
    esac
done

if [[ "${START_CHECK_HELP}" != true && "${START_CHECK_ARGUMENTS_VALID}" == true \
    && "${START_CHECK_PORT}" =~ ^[0-9]+$ && -n "${START_CHECK_HOST}" ]]; then
    START_CHECK_PROBE_HOST="${START_CHECK_HOST}"
    case "${START_CHECK_PROBE_HOST}" in
        0.0.0.0) START_CHECK_PROBE_HOST="127.0.0.1" ;;
        ::) START_CHECK_PROBE_HOST="::1" ;;
    esac
    if (exec 9<>"/dev/tcp/${START_CHECK_PROBE_HOST}/${START_CHECK_PORT}") 2>/dev/null; then
        printf '%s: port %s on %s is already accepting connections; refusing duplicate startup\n' \
            "${START_APP_NAME}" "${START_CHECK_PORT}" "${START_CHECK_HOST}" >&2
        exit 1
    fi
fi

start_common_main "$@"
