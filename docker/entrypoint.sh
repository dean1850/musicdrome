#!/bin/sh
# Boot sequence: refresh yt-dlp, drop to PUID/PGID, start the server.
set -e

# Root by default, because the common case for MUSIC_DIR is a network share or
# a media directory owned by root, which a dropped-privilege process cannot
# write to — and that failure lands at the very end of a download, after the
# audio has been fetched and encoded.
#
# The tradeoff is real: downloaded files end up owned by root. UMASK keeps them
# readable by everyone else, so Plex, Navidrome or Jellyfin can still serve
# them. Set PUID/PGID to your own id in .env if you would rather not run as
# root — see .env.example.
PUID="${PUID:-0}"
PGID="${PGID:-0}"
umask "${UMASK:-022}"

start() {
    exec uvicorn app.main:app \
        --host "${MUSICDROME_HOST:-0.0.0.0}" \
        --port "${MUSICDROME_PORT:-3046}" \
        --log-level "${MUSICDROME_LOG_LEVEL:-info}" \
        --no-access-log
}

# Second pass, re-executed through gosu: everything below has already run.
if [ -n "${MUSICDROME_DROPPED:-}" ]; then
    start
fi

# YouTube changes its player and format handling every few months, which breaks
# whatever yt-dlp version an image was built with. A fix normally ships within
# days, so a restart is enough to pick it up — that only works if the container
# refreshes yt-dlp on the way up. Set YTDLP_AUTO_UPDATE=false to pin to the
# version baked into the image instead.
if [ "${YTDLP_AUTO_UPDATE:-true}" = "true" ]; then
    echo "Updating yt-dlp (set YTDLP_AUTO_UPDATE=false to skip)"
    # Never fatal: an offline or rate-limited boot keeps the pinned version
    # rather than refusing to start.
    # [default] keeps yt-dlp-ejs in step with yt-dlp itself; upgrading the one
    # without the other is how you end up with a solver the extractor cannot use.
    if pip install --no-cache-dir --disable-pip-version-check --quiet \
            --timeout 20 --retries 2 --upgrade "yt-dlp[default]"; then
        echo "yt-dlp is now $(yt-dlp --version 2>/dev/null || echo unknown)"
    else
        echo "WARNING: could not update yt-dlp — continuing with the bundled version"
    fi
fi

if [ "$(id -u)" != "0" ] || [ "$PUID" = "0" ]; then
    echo "Musicdrome running as $(id -u):$(id -g) (umask ${UMASK:-022})"
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
MUSICDROME_DROPPED=1
export MUSICDROME_DROPPED
exec gosu "$PUID:$PGID" /usr/local/bin/entrypoint.sh
