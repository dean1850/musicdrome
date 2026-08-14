#!/bin/sh
# Boot sequence: refresh yt-dlp, drop to PUID/PGID, start the server.
set -e

# Root by default, and that default is about compatibility rather than being
# the right answer: a MUSIC_DIR that is a network share or a root-owned media
# directory is the one setup that works with no configuration at all, and the
# alternative failure lands at the very end of a download, after the audio has
# been fetched and encoded.
#
# Running as yourself is better practice and fully supported — set PUID/PGID in
# .env and everything below adapts, including handing an existing /config to
# the new uid. What it cannot do is change who owns MUSIC_DIR: on a local disk
# that is `chown -R`, and on a CIFS or NFS mount ownership comes from the mount
# options (uid=/gid= in fstab), where chown is a no-op. Get that right and the
# files Musicdrome creates land owned by you, which is the whole point.
#
# Either way UMASK keeps them readable by everyone else, so Plex, Navidrome or
# Jellyfin can still serve them. See .env.example.
PUID="${PUID:-0}"
PGID="${PGID:-0}"
umask "${UMASK:-022}"

# gosu deliberately does not touch the environment, so HOME survives the drop
# still pointing at root's (tianon/gosu#3, #14). Every tool that caches under
# ~ then tries to write a directory the new uid does not own. yt-dlp's is the
# one that costs something: its player and EJS script cache lives in
# $XDG_CACHE_HOME (falling back to ~/.cache), so at any PUID but 0 it silently
# stops persisting and every boot re-fetches and re-solves what it already had.
#
# /config is the answer for both, because it is the one path guaranteed to be
# writable by whoever we end up as — it is chowned to PUID/PGID below. Setting
# it for the root case too is not just tidiness: the cache lands in the mounted
# volume, so it now survives a restart instead of dying with the container.
HOME=/config
XDG_CACHE_HOME=/config/.cache
export HOME XDG_CACHE_HOME

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
    #
    # Both extras are named on purpose. [default] keeps yt-dlp-ejs in step with
    # yt-dlp itself; upgrading the one without the other is how you end up with
    # a solver the extractor cannot use. [curl-cffi] does the same job for the
    # impersonation handler, whose supported version window is a hard gate that
    # moves with yt-dlp — upgrading yt-dlp alone can leave an already-installed
    # curl_cffi outside it, which turns every download into 'Impersonate target
    # "chrome" is not available'. Naming it here lets pip put curl_cffi back
    # inside the window on the next restart, including downgrading it.
    if pip install --no-cache-dir --disable-pip-version-check --quiet \
            --timeout 20 --retries 2 --upgrade "yt-dlp[default,curl-cffi]"; then
        echo "yt-dlp is now $(yt-dlp --version 2>/dev/null || echo unknown)"
    else
        echo "WARNING: could not update yt-dlp — continuing with the bundled version"
    fi
fi

# Made before the drop so the chown below covers them. Left to the first tool
# that wants one, they would be created by whoever gets there first — as root
# on a still-root pass, and then be unwritable the next time PUID changes.
mkdir -p "$XDG_CACHE_HOME" "${DENO_DIR:-/config/.deno}" 2>/dev/null || true

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
# large read-only mount, and recursively chowning someone's library is rude —
# on a network share it is also a no-op, since ownership there comes from the
# mount options rather than from anything the container can change.
#
# This is what makes PUID changeable rather than a one-way door: switching from
# root to 1000 hands the existing database, caches and scratch files to the new
# uid on the next boot instead of leaving a /config it cannot open.
chown -R "$PUID:$PGID" /config 2>/dev/null || true

echo "Musicdrome running as ${PUID}:${PGID}"
MUSICDROME_DROPPED=1
export MUSICDROME_DROPPED
exec gosu "$PUID:$PGID" /usr/local/bin/entrypoint.sh
