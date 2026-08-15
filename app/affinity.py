"""The second input, applied in code.

The AI is told about your hearted tracks and asked to weigh them, and mostly it
does. "Mostly" is the problem. One AI call per scan is what makes a local 8B
model on Ollama a reasonable backend here rather than a compromise — and an 8B
model handed a new section of the prompt will sometimes read it, sometimes
paraphrase it back at you in the reason field without letting it move the
number, and sometimes ignore it outright. There is no way to tell which
happened from the answer alone.

So the hearts are applied twice: described in the prompt, where a capable model
can use them properly, and then added again here, where the arithmetic is
visible and does not depend on the model having cooperated. What this half
contributes is recorded on the row, so a card that scored 88 can say which part
of that was the model and which part was you.

**Why this is a small number.** The boost is capped at
:data:`MAX_BOOST` points, which is deliberately not enough to carry a bad
recommendation into auto-download territory on its own. The model has heard
your whole listening history and this has heard which artists you starred; it
is a thumb on the scale, and a thumb is the correct amount of pressure. A
larger boost would turn every scan into a list of tracks by the six artists you
have hearted, which is the opposite of discovery and is what the taste profile
already does well without help.

**What counts.** Four signals, summed and then capped:

* the suggested artist is one you have hearted — the strongest, and the only
  one that is a direct statement about that artist;
* the artist the recommendation came *from* is one you have hearted, which says
  the model reached somewhere sensible even if it landed on a stranger;
* the artist is among the ones you play most from your own library, which is
  the Navidrome play-count signal and catches listening that never scrobbled;
* the genre overlaps the genres you heart, the weakest and broadest of the
  four.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import config, db
from .norm import artist_key

log = logging.getLogger(__name__)

# Points per signal. Ordered by how direct a statement about *this artist* each
# one is, which is the only defensible way to rank them.
LOVED_ARTIST = 12
LOVED_SEED = 8
PLAYED_ARTIST = 6
LOVED_GENRE = 4

# The ceiling on everything above combined. See the module docstring: this is
# the whole reason the feature does not swamp the taste profile.
MAX_BOOST = 15

# How many artists count as "plays a lot from their own library", and the floor
# a play count has to clear to be evidence of anything. One play is a track
# that got as far as being opened once.
PLAYED_ARTIST_LIMIT = 60
PLAYED_ARTIST_MIN = 5

# Genres are the broadest signal and the easiest to make meaningless: a library
# of any size has hearted something in almost every genre once. Requiring two
# keeps it to genres you return to.
LOVED_GENRE_LIMIT = 12
LOVED_GENRE_MIN = 2


def fold_genre(value: Any) -> str:
    """A genre name as the key both sides compare on.

    Folded in Python rather than in SQL, which is the whole point of it having
    its own function. SQLite's ``lower()`` is ASCII-only by default: it leaves
    ``Électronique`` exactly as it found it, while Python lowercases the tag
    coming from Last.fm to ``électronique``, and the two never meet. The genre
    boost then silently never fires for any genre outside ASCII — no error, no
    log line, just a signal that quietly does nothing.
    """
    return str(value or "").strip().casefold()


def count_genres(rows: Iterable[Any], minimum: int, limit: int) -> dict[str, int]:
    """``{genre: hearts}`` for the most-hearted genres, folded and re-summed.

    Two spellings that fold to the same name are one genre, so the counts are
    added together here rather than left as separate rows.
    """
    counts: dict[str, int] = {}
    for row in rows:
        name = fold_genre(row["genre"])
        if name:
            counts[name] = counts.get(name, 0) + int(row["hearts"])
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return {name: hearts for name, hearts in ranked[:limit] if hearts >= minimum}


@dataclass
class Affinity:
    """A snapshot of what you have hearted, read once per scan.

    Held as plain dicts rather than queried per suggestion because a scan scores
    forty of these in a row and the underlying tables do not change while it
    runs.
    """

    loved_artists: dict[str, int] = field(default_factory=dict)
    loved_genres: dict[str, int] = field(default_factory=dict)
    played_artists: dict[str, int] = field(default_factory=dict)
    loved_tracks: int = 0

    def __bool__(self) -> bool:
        """Whether there is anything here to score against."""
        return bool(self.loved_artists or self.played_artists)

    def boost(
        self, artist: str, seed: str = "", tags: Iterable[str] = ()
    ) -> tuple[int, str]:
        """``(points, why)`` for one recommendation.

        ``why`` is written for a tooltip on the card: short, second person, and
        naming the specific artist or genre rather than the rule that fired.
        An empty string means nothing fired, and callers should not display a
        breakdown at all rather than displaying an empty one.
        """
        if not self:
            return 0, ""

        points = 0
        clauses: list[str] = []

        key = artist_key(artist)
        loved_here = self.loved_artists.get(key, 0)
        if loved_here:
            points += LOVED_ARTIST
            clauses.append(f"you have hearted {_tracks(loved_here)} by {artist}")

        # Only when it is a *different* artist. A model that names the
        # suggestion's own artist as the seed is not offering a second piece of
        # evidence, and paying twice for one fact is how a scoring rule stops
        # meaning anything.
        seed_key = artist_key(seed) if seed else ""
        if seed_key and seed_key != key:
            loved_seed = self.loved_artists.get(seed_key, 0)
            if loved_seed:
                points += LOVED_SEED
                clauses.append(f"it came from {seed.strip()}, who you have hearted")

        if not loved_here and self.played_artists.get(key, 0):
            points += PLAYED_ARTIST
            clauses.append(f"{artist} is among your most played in Navidrome")

        matched = self._genres(tags)
        if matched:
            points += LOVED_GENRE
            clauses.append(f"you heart {' and '.join(matched)}")

        if not points:
            return 0, ""
        return min(points, MAX_BOOST), ("; ".join(clauses))[:400]

    def _genres(self, tags: Iterable[str]) -> list[str]:
        """The two most-hearted genres this recommendation shares with you."""
        # A bare string is an Iterable[str] of single characters, so it would
        # be accepted and quietly match nothing. Worth a line, because tags are
        # stored on the suggestions row as one comma-joined string and anything
        # rescoring from there would hand over exactly that.
        if isinstance(tags, str):
            tags = tags.split(",")
        hits = [
            (self.loved_genres[name], name)
            for name in {fold_genre(tag) for tag in tags if fold_genre(tag)}
            if name in self.loved_genres
        ]
        return [name for _, name in sorted(hits, reverse=True)[:2]]


def load() -> Affinity:
    """Read the current heart and play-count picture out of SQLite.

    Returns an empty :class:`Affinity` — one that boosts nothing — when
    Navidrome is not configured or has not synced yet, so every caller can run
    unconditionally instead of branching on whether the feature is set up.
    """
    if not config.navidrome_configured():
        return Affinity()

    with db.connect() as conn:
        loved = conn.execute(
            "SELECT artist_key, artist, COUNT(*) AS hearts FROM navidrome_tracks "
            "WHERE starred = 1 AND artist_key != '' GROUP BY artist_key"
        ).fetchall()

        # Grouped exactly, then folded and re-ranked in Python — see
        # :func:`fold_genre` for why SQL cannot do the folding.
        genres = conn.execute(
            "SELECT genre, COUNT(*) AS hearts FROM navidrome_tracks "
            "WHERE starred = 1 AND genre != '' GROUP BY genre"
        ).fetchall()

        played = conn.execute(
            "SELECT artist_key, SUM(play_count) AS plays FROM navidrome_tracks "
            "WHERE artist_key != '' GROUP BY artist_key "
            "HAVING plays >= ? ORDER BY plays DESC LIMIT ?",
            (PLAYED_ARTIST_MIN, PLAYED_ARTIST_LIMIT),
        ).fetchall()

        total = conn.execute(
            "SELECT COUNT(*) AS n FROM navidrome_tracks WHERE starred = 1"
        ).fetchone()

    return Affinity(
        loved_artists={row["artist_key"]: row["hearts"] for row in loved},
        loved_genres=count_genres(genres, LOVED_GENRE_MIN, LOVED_GENRE_LIMIT),
        played_artists={row["artist_key"]: row["plays"] for row in played},
        loved_tracks=total["n"] if total else 0,
    )


def _tracks(count: int) -> str:
    return "1 track" if count == 1 else f"{count} tracks"


def apply(base: int, artist: str, seed: str = "", tags: Iterable[str] = (),
          picture: Affinity | None = None) -> dict[str, Any]:
    """Blend one model score with the hearts, as the columns to store.

    The final match is clamped to 100 rather than allowed past it, and the
    parts are returned unclamped alongside — so a card that would have scored
    108 records honestly that the model said 96 and the hearts added 12, rather
    than looking like a 100 nobody can account for.
    """
    picture = load() if picture is None else picture
    points, why = picture.boost(artist, seed=seed, tags=tags)
    return {
        "match": max(0, min(100, base + points)),
        "match_base": base,
        "affinity": points,
        "affinity_reason": why,
    }
