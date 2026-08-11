#!/bin/sh
# Drop to PUID/PGID before starting, so files written into your music library
# are owned by you rather than by root. Set PUID=0 to stay root.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

start() {
    exec uvicorn app.main:app \
        --host "${MUSICDROME_HOST:-0.0.0.0}" \
        --port "${MUSICDROME_PORT:-3046}" \
        --log-level "${MUSICDROME_LOG_LEVEL:-info}" \
        --no-access-log
}

if [ "$(id -u)" != "0" ] || [ "$PUID" = "0" ]; then
    start
fi

if ! getent group musicdrome >/dev/null 2>&1; then
    groupadd -o -g "$PGID" musicdrome
fi
if ! id -u musicdrome >/dev/null 2>&1; then
    useradd -o -u "$PUID" -g "$PGID" -d /config -s /bin/sh musicdrome
fi

# Only ever chown what we own. The music directory is left alone: it may be a
# large read-only mount, and recursively chowning someone's library is rude.
chown -R "$PUID:$PGID" /config 2>/dev/null || true

echo "Musicdrome running as ${PUID}:${PGID}"
exec gosu "$PUID:$PGID" /usr/local/bin/entrypoint.sh
