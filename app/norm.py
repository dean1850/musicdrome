"""Normalising artist and track names into comparison keys.

Every "have I already got this?" decision in Musicdrome runs through here. The
same recording reaches us spelled several ways — Last.fm reports what the player
sent, MusicBrainz reports the canonical credit, the AI writes whatever it
remembers, YouTube Music adds "(Remastered 2011)" — so raw string equality
misses matches constantly and the same track gets suggested every scan.

The keys are deliberately lossy. ``track_key`` exists to answer "is this the
same song?", not to be displayed or reversed.
"""

from __future__ import annotations

import re
import unicodedata

# Trailing parenthetical noise that does not change which recording this is.
_PAREN_NOISE = re.compile(
    r"[\(\[]\s*(?:"
    r"\d{4}\s+)?(?:digital\s+|\d{4}\s+)?(?:remaster(?:ed)?|remastered\s+version|"
    r"deluxe(?:\s+edition)?|bonus\s+track|album\s+version|single\s+version|"
    r"radio\s+edit|explicit|clean|mono|stereo|expanded(?:\s+edition)?|"
    r"anniversary(?:\s+edition)?|reissue|original\s+mix"
    r")\s*[^\)\]]*[\)\]]",
    re.IGNORECASE,
)

# Featured-artist credits, in every spelling players use.
_FEAT = re.compile(
    r"\s*[\(\[]?\s*\b(?:feat|ft|featuring|with)\b\.?\s+[^\)\]]*[\)\]]?\s*$",
    re.IGNORECASE,
)

_THE_PREFIX = re.compile(r"^the\s+", re.IGNORECASE)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")

SEPARATOR = "\x1f"  # joins artist and title inside a track key


def _fold(value: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("&", " and ")
    text = _PUNCT.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def artist_key(artist: str) -> str:
    """A comparison key for an artist credit.

    Drops a leading "The" so *The Beatles* and *Beatles* agree, and keeps only
    the primary credit from a collaboration list.
    """
    name = _FEAT.sub("", artist or "")
    # Collaboration separators. The word-like ones ("x", "vs") need whitespace on
    # both sides, or "Malcolm X" loses its name.
    name = re.split(r"\s*[,;/&]\s*|\s+(?:vs\.?|x)\s+", name, maxsplit=1)[0]
    return _THE_PREFIX.sub("", _fold(name))


def title_key(title: str) -> str:
    """A comparison key for a track title, minus edition and feature noise."""
    text = _PAREN_NOISE.sub("", title or "")
    text = _FEAT.sub("", text)
    text = re.sub(r"\s*-\s*(?:remaster(?:ed)?|\d{4}\s+remaster(?:ed)?|"
                  r"radio\s+edit|album\s+version|single\s+version)\b.*$",
                  "", text, flags=re.IGNORECASE)
    return _fold(text)


def track_key(artist: str, title: str) -> str:
    """The identity Musicdrome uses for "same song" across every source."""
    return f"{artist_key(artist)}{SEPARATOR}{title_key(title)}"


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(value: str, fallback: str) -> str:
    """A path component that is safe on every filesystem we might be mounted on."""
    cleaned = _UNSAFE.sub("", (value or "").strip()).rstrip(". ").strip()
    return (cleaned or fallback)[:120]
