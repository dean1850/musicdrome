"""Seed a database for the browser tests.

Writes plays and suggestions straight into SQLite so the UI has something to
render without a Last.fm key, an AI backend or a network connection.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import db  # noqa: E402
from app.norm import artist_key, track_key  # noqa: E402

PLAYS = [
    ("Radiohead", "Karma Police"),
    ("Radiohead", "No Surprises"),
    ("Portishead", "Glory Box"),
    ("Massive Attack", "Teardrop"),
]

# The last three fields are the match breakdown: what the model said, what the
# Navidrome hearts added, and why. Two of these cards are boosted and two are
# not, so the UI has both states to render.
SUGGESTIONS = [
    ("Boards of Canada", "Roygbiv", "Music Has the Right to Children", 94,
     "You have been deep in Massive Attack, and this is the same patient, hazy end of electronica.",
     "downtempo,idm", 82, 12, "you have hearted 3 tracks by Boards of Canada"),
    ("Mazzy Star", "Fade Into You", "So Tonight That I Might See", 88,
     "Your Portishead listening points straight at this kind of slow, smoky songwriting.",
     "dream pop,shoegaze", 88, 0, ""),
    ("Talk Talk", "New Grass", "Laughing Stock", 76,
     "A sideways step from Radiohead into the record that taught them how to end an album.",
     "post-rock,art rock", 72, 4, "you heart post-rock"),
    ("Burial", "Archangel", "Untrue", 61,
     "A speculative pick — the Massive Attack thread, twenty years later and colder.",
     "dubstep,downtempo", 61, 0, ""),
]

# Finished downloads, so the table has rows to lay out. All of them succeeded:
# a failed one would make the "retry all failed is hidden until something has
# failed" test vacuous.
#
# The first is deliberately pathological. A DJ mix carries the whole tracklist
# in its title and every credited artist in its artist field, which is how the
# downloads table came to be 2583px wide at every viewport size — one row set
# the width of a column and pushed the other seven off-screen. It is seeded so
# that a regression in the column widths fails a test rather than a bug report.
LONG_TITLE = (
    "The Sound of Tropical House: Waves (Robin Schulz radio edit) / Sonnentanz "
    "(Sun Don't Shine) (vocal radio edit) / Sugar / Changes / Perfect Strangers"
)
LONG_ARTIST = (
    "Mr. Probz / Klangkarussell feat. Will Heard / Robin Schulz feat. Francesco "
    "Yates / Faul & Wad vs. PNAU / Jonas Blue feat. JP Cooper"
)

DOWNLOADS = [
    (LONG_ARTIST, LONG_TITLE, "Mastermix Issue 365",
     f"/music/{LONG_ARTIST[:60]}/Mastermix Issue 365/{LONG_TITLE[:60]}.opus",
     7_340_032, "opus", 133, "copied"),
    ("Tinlicker", "Breathe", "Breathe", "/music/Tinlicker/Breathe/Breathe.opus",
     4_718_592, "opus", 135, "copied"),
    ("BUNT.", "Midnight City", "BUNT. EP", "/music/BUNT/BUNT. EP/Midnight City.opus",
     3_774_874, "opus", 131, "converted"),
    # No codec recorded, which is its own thing to render.
    ("Solven", "Moments", "", "/music/Solven/Singles/Moments.opus", 3_565_158, "", 0, ""),
    ("Robin Schulz", "Sugar", "Prayer", "/music/Robin Schulz/Prayer/Sugar.mp3",
     8_912_896, "", 0, ""),
    ("Kolya Funk", "Universe", "Universe", "/music/Kolya Funk/Universe/Universe.mp3",
     6_081_740, "", 0, ""),
    ("Cale", "Echoes", "Echoes", "/music/Cale/Echoes/Echoes.opus",
     3_670_016, "opus", 148, "copied"),
    # A single: no album, so the album cell has nothing to show.
    ("Dream Chaos", "Heartbeat", "", "/music/Dream Chaos/Singles/Heartbeat.opus",
     3_145_728, "opus", 130, "copied"),
]

# What Navidrome would have reported. Hearts feed the Connections panel and the
# exclusion set; the un-hearted row is there so "owned" and "hearted" stay
# visibly different things.
NAVIDROME_TRACKS = [
    ("Boards of Canada", "Olson", True, 31),
    ("Boards of Canada", "Turquoise Hexagon Sun", True, 22),
    ("Boards of Canada", "Dayvan Cowboy", True, 18),
    ("Portishead", "Roads", False, 9),
]


def main() -> None:
    db.init()
    now = db.now()

    with db.connect() as conn:
        conn.execute("DELETE FROM plays")
        conn.execute("DELETE FROM suggestions")
        conn.execute("DELETE FROM downloads")
        conn.execute("DELETE FROM scans")
        conn.execute("DELETE FROM navidrome_tracks")
        conn.execute("DELETE FROM sync_state")

        for index, (artist, title) in enumerate(PLAYS):
            for repeat in range(4):
                conn.execute(
                    "INSERT OR IGNORE INTO plays (artist, title, album, artist_key, track_key, "
                    "played_at, source) VALUES (?, ?, '', ?, ?, ?, 'lastfm')",
                    (artist, title, artist_key(artist), track_key(artist, title),
                     now - (index * 3600) - (repeat * 86400)),
                )

        cursor = conn.execute(
            "INSERT INTO scans (started_at, finished_at, status, trigger, provider, model, "
            "requested, returned, kept) VALUES (?, ?, 'ok', 'manual', 'ollama', 'llama3.1', 4, 4, 4)",
            (now - 600, now - 540),
        )
        scan_id = cursor.lastrowid

        for artist, title, album, match, reason, tags, base, boost, why in SUGGESTIONS:
            conn.execute(
                "INSERT INTO suggestions (scan_id, track_key, artist, title, album, match, "
                "match_base, affinity, affinity_reason, reason, tags, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)",
                (scan_id, track_key(artist, title), artist, title, album, match,
                 base, boost, why, reason, tags, now),
            )

        # Older than anything queued during a test, so a newly queued download
        # still sorts to the top of the table.
        for index, (artist, title, album, path, size, codec, abr, encoded) in enumerate(DOWNLOADS):
            conn.execute(
                "INSERT INTO downloads (track_key, artist, title, album, path, bytes, "
                "source_codec, source_abr, encoded, status, created_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'done', ?, ?)",
                (track_key(artist, title), artist, title, album, path, size, codec, abr,
                 encoded, now - 7200 - index * 60, now - 7100 - index * 60),
            )

        for index, (artist, title, starred, plays) in enumerate(NAVIDROME_TRACKS):
            conn.execute(
                "INSERT INTO navidrome_tracks (id, artist, title, album, artist_key, track_key, "
                "genre, year, starred, starred_at, rating, play_count, played_at, synced_at) "
                "VALUES (?, ?, ?, '', ?, ?, 'idm', 1998, ?, ?, 0, ?, ?, ?)",
                (f"nd-{index}", artist, title, artist_key(artist), track_key(artist, title),
                 int(starred), now - 86400 if starred else 0, plays, now - 3600, now),
            )
        conn.execute(
            "INSERT INTO sync_state (source, cursor, synced_at, error) VALUES (?, 0, ?, '')",
            ("navidrome", now - 120),
        )

    print(
        f"seeded {len(SUGGESTIONS)} suggestions, {len(PLAYS) * 4} plays, "
        f"{len(DOWNLOADS)} downloads and {len(NAVIDROME_TRACKS)} Navidrome tracks"
    )


if __name__ == "__main__":
    main()
