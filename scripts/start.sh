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
# Auto-reload is on by default. Use --no-reload for demo/batch/crawler runs: a reload drops the in-flight request.
START_DEFAULT_RELOAD=true
START_SKIP_SYNC="${START_SKIP_SYNC:-true}"
START_PROJECT_ROOT="${PROJECT_ROOT}"
# The host-wide uv cache may be owned by another account. Keep an explicit
# caller override, otherwise use the standard user-writable cache location.
export UV_CACHE_DIR="${UV_CACHE_DIR:-${XDG_CACHE_HOME:-${HOME}/.cache}/uv}"

SHARED_START="${AECS4U_START_COMMON:-${SCRIPT_DIR}/start-common.sh}"
if [[ ! -r "${SHARED_START}" ]]; then
    SHARED_START="${PROJECT_ROOT}/../bin/start-common.sh"
fi
SHARED_START="${PROJECT_ROOT}/../bin/start-common.sh"
source "${SHARED_START}"

START_ENV="dev"
START_ARGUMENTS=()
START_SHOW_HELP=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --env)
            if [[ $# -lt 2 || -z "$2" ]]; then
                printf '%s: --env requires dev, test, staging, or production\n' "${START_APP_NAME}" >&2
                exit 2
            fi
            START_ENV="$2"
            shift 2
            ;;
        --env=*)
            START_ENV="${1#--env=}"
            shift
            ;;
        -h|--help)
            START_SHOW_HELP=true
            START_ARGUMENTS+=("$1")
            shift
            ;;
        --)
            START_ARGUMENTS+=("$@")
            break
            ;;
        *)
            START_ARGUMENTS+=("$1")
            shift
            ;;
    esac
done

case "${START_ENV,,}" in
    dev|development)
        START_RUNTIME_ENVIRONMENT="development"
        START_PROFILE_DEBUG=true
        START_PROFILE_LOG_LEVEL=DEBUG
        START_PROFILE_RELOAD=true
        ;;
    test|testing)
        START_RUNTIME_ENVIRONMENT="testing"
        START_PROFILE_DEBUG=false
        START_PROFILE_LOG_LEVEL=INFO
        START_PROFILE_RELOAD=false
        ;;
    staging|stage)
        START_RUNTIME_ENVIRONMENT="staging"
        START_PROFILE_DEBUG=false
        START_PROFILE_LOG_LEVEL=INFO
        START_PROFILE_RELOAD=false
        ;;
    prod|production)
        START_RUNTIME_ENVIRONMENT="production"
        START_PROFILE_DEBUG=false
        START_PROFILE_LOG_LEVEL=INFO
        START_PROFILE_RELOAD=false
        ;;
    *)
        printf '%s: invalid environment %q (choose dev, test, staging, or production)\n' \
            "${START_APP_NAME}" "${START_ENV}" >&2
        exit 2
        ;;
esac

export ENVIRONMENT="${START_RUNTIME_ENVIRONMENT}"
export LOGFIRE_ENVIRONMENT="${START_RUNTIME_ENVIRONMENT}"
export DEBUG="${START_DEBUG:-${START_PROFILE_DEBUG}}"
export LOG_LEVEL="${START_APP_LOG_LEVEL:-${START_PROFILE_LOG_LEVEL}}"
START_DEFAULT_RELOAD="${START_PROFILE_RELOAD}"

if [[ "${START_SHOW_HELP}" == true ]]; then
    start_common_usage
    printf '\nSISTER environment option:\n  --env ENV  dev (default, reload on), test, staging, or production (reload off)\n'
    exit 0
fi

# Uvicorn runs the application lifespan before the final bind. Catch an
# already-running local instance first so a duplicate launch does not perform
# database/browser startup and then fail with EADDRINUSE.
START_CHECK_HOST="${START_HOST:-${HOST:-${START_DEFAULT_HOST}}}"
START_CHECK_PORT="${START_PORT:-${PORT:-${START_DEFAULT_PORT}}}"
START_CHECK_HELP=false
START_CHECK_ARGUMENTS_VALID=true
START_CHECK_ARGS=("${START_ARGUMENTS[@]}")
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

start_common_main "${START_ARGUMENTS[@]}"
