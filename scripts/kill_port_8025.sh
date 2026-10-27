#!/usr/bin/env bash
set -Eeuo pipefail

PORT=8025
WAIT_ATTEMPTS=20
WAIT_INTERVAL=0.25

if ! command -v fuser >/dev/null 2>&1; then
    printf 'Cannot stop the app on port %s: fuser is not installed.\n' "${PORT}" >&2
    exit 1
fi

if ! fuser -n tcp "${PORT}" >/dev/null 2>&1; then
    printf 'No process is listening on port %s.\n' "${PORT}"
    exit 0
fi

printf 'Sending SIGTERM to the process listening on port %s...\n' "${PORT}"
if ! fuser -TERM -k -n tcp "${PORT}" >/dev/null 2>&1; then
    printf 'Could not send SIGTERM to the listener on port %s. Check process ownership.\n' "${PORT}" >&2
    exit 1
fi

for ((attempt = 0; attempt < WAIT_ATTEMPTS; attempt++)); do
    if ! fuser -n tcp "${PORT}" >/dev/null 2>&1; then
        printf 'Stopped the app on port %s.\n' "${PORT}"
        exit 0
    fi
    sleep "${WAIT_INTERVAL}"
done

printf 'The listener did not stop after SIGTERM; sending SIGKILL on port %s...\n' "${PORT}"
if ! fuser -KILL -k -n tcp "${PORT}" >/dev/null 2>&1; then
    printf 'Could not send SIGKILL to the listener on port %s.\n' "${PORT}" >&2
    exit 1
fi

for ((attempt = 0; attempt < WAIT_ATTEMPTS; attempt++)); do
    if ! fuser -n tcp "${PORT}" >/dev/null 2>&1; then
        printf 'Stopped the app on port %s.\n' "${PORT}"
        exit 0
    fi
    sleep "${WAIT_INTERVAL}"
done

printf 'A process is still listening on port %s.\n' "${PORT}" >&2
exit 1
