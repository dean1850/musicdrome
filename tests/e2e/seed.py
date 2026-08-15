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
        f"seeded {len(SUGGESTIONS)} suggestions, {len(PLAYS) * 4} plays "
        f"and {len(NAVIDROME_TRACKS)} Navidrome tracks"
    )


if __name__ == "__main__":
    main()
