#!/usr/bin/env bash
# Musicdrome container entrypoint.
#   serve  — run the API server (default)
#   scan   — run a one-off library scan and exit
#   shell  — drop into bash
set -euo pipefail

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
PORT="${MUSICDROME_PORT:-4533}"
HOST="${MUSICDROME_HOST:-0.0.0.0}"
WORKERS="${MUSICDROME_WORKERS:-1}"
LOG_LEVEL="${MUSICDROME_LOG_LEVEL:-info}"

log() { printf '[entrypoint] %s\n' "$*" >&2; }

# ── Map the container user onto the host's uid/gid so files written into the
#    music library are owned by you and not by root.
if [ "$(id -u)" = "0" ]; then
    if ! getent group musicdrome >/dev/null 2>&1; then
        groupadd -g "$PGID" musicdrome 2>/dev/null || groupadd musicdrome
    fi
    if ! id -u musicdrome >/dev/null 2>&1; then
        useradd -u "$PUID" -g "$PGID" -d /app -s /bin/bash musicdrome 2>/dev/null \
            || useradd -g musicdrome -d /app -s /bin/bash musicdrome
    fi

    # Only chown the dirs we own outright — never recurse into /music, which
    # may be huge and is frequently mounted read-only.
    for d in /config /cache /podcasts /downloads; do
        mkdir -p "$d"
        chown "$PUID:$PGID" "$d" 2>/dev/null || true
    done

    EXEC_PREFIX=(gosu "$PUID:$PGID")
else
    EXEC_PREFIX=()
fi

run_as() {
    if [ "${#EXEC_PREFIX[@]}" -gt 0 ]; then
        exec "${EXEC_PREFIX[@]}" "$@"
    else
        exec "$@"
    fi
}

case "${1:-serve}" in
    serve)
        log "starting Musicdrome on ${HOST}:${PORT}"
        run_as python -m uvicorn musicdrome.main:app \
            --host "$HOST" \
            --port "$PORT" \
            --workers "$WORKERS" \
            --log-level "$LOG_LEVEL" \
            --proxy-headers \
            --forwarded-allow-ips '*'
        ;;
    scan)
        log "running one-off library scan"
        run_as python -m musicdrome.cli scan "${@:2}"
        ;;
    cli)
        run_as python -m musicdrome.cli "${@:2}"
        ;;
    shell)
        run_as /bin/bash
        ;;
    *)
        run_as "$@"
        ;;
esac
