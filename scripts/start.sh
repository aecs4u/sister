#!/bin/bash
# sister — start script
# Registry: /data/aecs4u.it/apps.json

set -e

# ── App configuration ──────────────────────────────────────────────────────────
APP_NAME="SISTER"
MODULE="sister.main:app"
DEFAULT_PORT=8025              # must match /data/aecs4u.it/apps.json
DEFAULT_HOST="0.0.0.0"
# ──────────────────────────────────────────────────────────────────────────────

GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo "Options:"
    echo "  -p, --port PORT   Port to listen on  (default: $DEFAULT_PORT)"
    echo "  -H, --host HOST   Host to bind to    (default: $DEFAULT_HOST)"
    echo "  --no-reload       Disable auto-reload"
    echo "  --help            Show this help"
    exit 0
}

RELOAD="--reload"
HOST=""
PORT=""

while [[ $# -gt 0 ]]; do
    case $1 in
        -p|--port)   PORT="$2"; shift 2 ;;
        -H|--host)   HOST="$2"; shift 2 ;;
        --no-reload) RELOAD=""; shift ;;
        --help)      usage ;;
        *) echo -e "${RED}Unknown option: $1${NC}"; usage ;;
    esac
done

HOST="${HOST:-$DEFAULT_HOST}"
PORT="${PORT:-$DEFAULT_PORT}"

cd "$(dirname "$0")/.."

[ ! -f .env ] && [ -f .env.example ] && {
    echo -e "${YELLOW}Creating .env from .env.example — edit it before restarting if needed${NC}"
    cp .env.example .env
}

export PYTHONPATH="$(pwd):${PYTHONPATH:-}"

echo -e "${BLUE}Starting ${APP_NAME} → http://${HOST}:${PORT}${NC}"

if [ -n "$RELOAD" ]; then
    # Only reload on Python file changes — avoid restarting on template/CSS/JS edits
    # which would kill the authenticated browser session
    exec uv run uvicorn "$MODULE" --host "$HOST" --port "$PORT" --workers 1 \
        $RELOAD --reload-include "*.py" --reload-exclude "templates/*" --reload-exclude "static/*"
else
    exec uv run uvicorn "$MODULE" --host "$HOST" --port "$PORT" --workers 1
fi
