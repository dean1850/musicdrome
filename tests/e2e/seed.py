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

SUGGESTIONS = [
    ("Boards of Canada", "Roygbiv", "Music Has the Right to Children", 94,
     "You have been deep in Massive Attack, and this is the same patient, hazy end of electronica.",
     "downtempo,idm"),
    ("Mazzy Star", "Fade Into You", "So Tonight That I Might See", 88,
     "Your Portishead listening points straight at this kind of slow, smoky songwriting.",
     "dream pop,shoegaze"),
    ("Talk Talk", "New Grass", "Laughing Stock", 76,
     "A sideways step from Radiohead into the record that taught them how to end an album.",
     "post-rock,art rock"),
    ("Burial", "Archangel", "Untrue", 61,
     "A speculative pick — the Massive Attack thread, twenty years later and colder.",
     "dubstep,downtempo"),
]


def main() -> None:
    db.init()
    now = db.now()

    with db.connect() as conn:
        conn.execute("DELETE FROM plays")
        conn.execute("DELETE FROM suggestions")
        conn.execute("DELETE FROM downloads")
        conn.execute("DELETE FROM scans")

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

        for artist, title, album, match, reason, tags in SUGGESTIONS:
            conn.execute(
                "INSERT INTO suggestions (scan_id, track_key, artist, title, album, match, "
                "reason, tags, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)",
                (scan_id, track_key(artist, title), artist, title, album, match, reason, tags, now),
            )

    print(f"seeded {len(SUGGESTIONS)} suggestions and {len(PLAYS) * 4} plays")


if __name__ == "__main__":
    main()
