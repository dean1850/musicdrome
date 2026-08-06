#!/usr/bin/env bash
# Start a Musicdrome instance against a throwaway library, for the E2E suite.
#
# Everything lives under tests/.tmp so a run never touches a real library, and
# the state is rebuilt from scratch each time so the tests stay deterministic.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TMP="$HERE/.tmp"
PORT="${E2E_PORT:-4599}"

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3)"
fi

# A fresh database every run; the generated audio is cached because writing it
# is the slow part.
rm -rf "$TMP/config" "$TMP/cache" "$TMP/podcasts" "$TMP/downloads"
mkdir -p "$TMP"/{music,config,cache,podcasts,downloads}

export MUSICDROME_TESTING=true
export MUSICDROME_HOST=127.0.0.1
export MUSICDROME_PORT="$PORT"
export MUSICDROME_MUSIC_DIR="$TMP/music"
export MUSICDROME_DATA_DIR="$TMP/config"
export MUSICDROME_CACHE_DIR="$TMP/cache"
export MUSICDROME_PODCAST_DIR="$TMP/podcasts"
export MUSICDROME_DOWNLOAD_DIR="$TMP/downloads"
export MUSICDROME_STATIC_DIR="${MUSICDROME_STATIC_DIR:-$ROOT/frontend/dist}"
export SECRET_KEY="e2e-test-secret-key-not-for-production-use"
export CREDENTIAL_ENCRYPTION_KEY=""
export DEFAULT_ADMIN_USERNAME="${E2E_ADMIN_USERNAME:-admin}"
export DEFAULT_ADMIN_PASSWORD="${E2E_ADMIN_PASSWORD:-testadmin123}"
export SCAN_ON_STARTUP=false
export ALLOW_OPEN_REGISTRATION=true
export AI_ENABLED=false
export LASTFM_ENABLED=false
export LISTENBRAINZ_ENABLED=false
export MUSICBRAINZ_ENABLED=false
export RECOMMENDATIONS_ENABLED=false
export ACQUISITION_ENABLED=true
export PODCAST_ENABLED=true
export LIDARR_ENABLED=false
export PYTHONPATH="$ROOT/backend"

"$PYTHON" "$HERE/seed_library.py" "$TMP/music" >&2

# The admin is created up front rather than left to the server's first-run
# bootstrap: imported playlists need an owner, and the scan below is what
# imports the seeded .m3u.
"$PYTHON" -m musicdrome.cli create-user "$DEFAULT_ADMIN_USERNAME" \
    --password "$DEFAULT_ADMIN_PASSWORD" --admin >&2

"$PYTHON" -m musicdrome.cli scan >&2

# The starter smart playlists were materialised against an empty library a
# moment ago; fill them in now that the tracks are indexed.
"$PYTHON" -m musicdrome.cli refresh playlists >&2

exec "$PYTHON" -m uvicorn musicdrome.main:app \
    --host 127.0.0.1 --port "$PORT" --log-level warning
