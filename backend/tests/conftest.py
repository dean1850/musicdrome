"""Point the whole package at a throwaway data directory.

``musicdrome.config`` reads the environment at import time and creates its
directories there and then, so this has to happen before anything imports it.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="musicdrome-tests-"))

os.environ.update(
    MUSICDROME_TESTING="true",
    MUSICDROME_MUSIC_DIR=str(_TMP / "music"),
    MUSICDROME_DATA_DIR=str(_TMP / "config"),
    MUSICDROME_CACHE_DIR=str(_TMP / "cache"),
    MUSICDROME_PODCAST_DIR=str(_TMP / "podcasts"),
    MUSICDROME_DOWNLOAD_DIR=str(_TMP / "downloads"),
    SECRET_KEY="test-secret-key-not-for-production",
    SCAN_ON_STARTUP="false",
    SCAN_WATCH_FILESYSTEM="false",
    AI_ENABLED="false",
    LASTFM_ENABLED="false",
    LISTENBRAINZ_ENABLED="false",
    MUSICBRAINZ_ENABLED="false",
)
