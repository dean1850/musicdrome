"""The discovery pipeline.

One scan is: pull new scrobbles, work out what you have been listening to, ask
the AI once for tracks you would like next, resolve each answer against
MusicBrainz, throw away anything you already have, and store the rest as cards.

It is one AI call per scan, not one per track. Forty recommendations for the
price of a single request is what makes a local 8B model on Ollama a reasonable
choice here rather than a compromise.

A scan runs on a background thread. :data:`_state` is what the UI polls to draw
the progress line, and :data:`_lock` is what stops two of them overlapping.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from . import affinity, ai, db, exclude, history
from .norm import artist_key, track_key
from .sources import lastfm, musicbrainz, navidrome

log = logging.getLogger(__name__)

_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False, "step": "", "done": 0, "total": 0, "scan_id": None,
}


SYSTEM_PROMPT = """You are a music recommender for one listener's personal collection.

You are given two different kinds of evidence about that listener, and they are
not worth the same.

PLAYS are what they listened to: most-played artists, most-played tracks, and
the artists they discovered most recently. This is a large, noisy signal. A high
play count can mean a record they love or an album that was on in the background
for a fortnight, and nothing in the data distinguishes the two.

HEARTS are what they went back and starred in their own music library. There are
far fewer of these and every one of them is deliberate — nobody stars a track by
accident, or because it was next in the queue. Treat a hearted artist or genre
as a much stronger statement of taste than any play count, and weight it
accordingly when you choose what to recommend and how confident you are.

Recommend individual studio tracks they do not yet have but would plausibly love.

Rules:
- Recommend real, released, individually downloadable studio recordings. Never
  invent a song, and never recommend a track you are not confident exists.
- Give the artist exactly as credited on the release, and the track title
  without any "(Remastered)", "(Live)" or featured-artist suffix.
- Do not recommend anything in the EXCLUDE list, and do not recommend a track
  that merely renames something in it.
- Aim for range: roughly half from artists adjacent to what they already play,
  and half genuine sideways steps into neighbouring scenes, eras or genres.
- At most two tracks by any one artist.
- "match" is your confidence, 0-100, that this specific listener will like this
  specific track. Use the whole range honestly — a speculative pick belongs in
  the 50s, and 90+ should mean you would be surprised if they disliked it. Let
  the hearts move this number, not just the choice of track: something in the
  same territory as what they heart deserves more confidence than the same
  track would earn from the play counts alone.
- "reason" is one short sentence in second person, naming what in their history
  led you here. Say so when it was something they hearted, rather than
  something they merely played. "seed" is the single artist from their history
  it came from.
"""

# Enforced, not merely described. Ollama compiles this into a grammar, so
# "additionalProperties": false is doing real work here: without it a small
# model invents fields — popularity, image_url, genre — and spends its token
# budget on them until the answer is cut off mid-value.
SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "artist": {"type": "string"},
                    "title": {"type": "string"},
                    "album": {"type": "string"},
                    "match": {"type": "integer", "minimum": 0, "maximum": 100},
                    "reason": {"type": "string"},
                    "seed": {"type": "string"},
                },
                "required": ["artist", "title", "match", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["recommendations"],
    "additionalProperties": False,
}

# What one recommendation costs to write out, roughly, including the JSON
# scaffolding around it and a sentence of reasoning inside it. Only used to
# size the request — see :func:`app.ai._context_window`.
TOKENS_PER_RECOMMENDATION = 120


def state() -> dict[str, Any]:
    return dict(_state)


# How much of the hearted set the prompt carries. Hearts are scarce enough that
# most libraries fit under this several times over; a listener who has starred
# two thousand tracks gets the most recent slice, which is the part that says
# where their taste is now.
LOVED_TRACKS_IN_PROMPT = 60
LOVED_ARTISTS_IN_PROMPT = 25


def build_prompt(profile: dict[str, Any], excluded_titles: list[str], batch_size: int) -> str:
    """The user turn: the listener's profile plus what not to suggest.

    The hearted sections come *before* the play counts, and are labelled as the
    stronger evidence in both places. Position is not a decoration here: a model
    reading a long prompt weights the top of it more heavily, and the whole
    point of the Navidrome data is that fifty deliberate hearts say more than
    fifty thousand plays.

    The exclusion list is truncated because a library of 20,000 tracks does not
    fit in a prompt and would drown the profile if it did. The full set is still
    applied in code after the model answers — this is a hint, not the guarantee.
    """
    lines = [
        f"Listening window: the last {profile['days']} days "
        f"({profile['plays']} plays across {profile['artists']} artists).",
    ]

    loved_tracks = profile.get("loved_tracks") or []
    loved_artists = profile.get("loved_artists") or []
    loved_genres = profile.get("loved_genres") or []
    library_top = profile.get("library_top_tracks") or []

    if loved_artists:
        lines += ["", "── HEARTS (deliberate, and the strongest signal here) ──"]
        lines += ["", "ARTISTS THEY HAVE HEARTED MOST:"]
        lines += [
            f"  {a['artist']} ({a['hearts']} hearted)"
            for a in loved_artists[:LOVED_ARTISTS_IN_PROMPT]
        ]

    if loved_tracks:
        lines += ["", "TRACKS THEY HEARTED (most recently hearted first):"]
        lines += [
            f"  {t['artist']} — {t['title']}" for t in loved_tracks[:LOVED_TRACKS_IN_PROMPT]
        ]

    if loved_genres:
        lines += ["", "GENRES THEY HEART:"]
        lines += [f"  {g['genre']} ({g['hearts']})" for g in loved_genres[:12]]

    if loved_artists or loved_tracks:
        lines += ["", "── PLAYS (larger, noisier) ──"]

    lines += ["", "MOST PLAYED ARTISTS:"]
    lines += [f"  {a['artist']} ({a['plays']})" for a in profile["top_artists"][:30]] or ["  (none)"]

    lines += ["", "MOST PLAYED TRACKS:"]
    lines += [f"  {t['artist']} — {t['title']} ({t['plays']})" for t in profile["top_tracks"][:30]] or ["  (none)"]

    if library_top:
        lines += [
            "",
            "MOST PLAYED FROM THEIR OWN LIBRARY (all time, counted by their music "
            "server rather than scrobbled):",
        ]
        lines += [f"  {t['artist']} — {t['title']} ({t['play_count']})" for t in library_top[:20]]

    if profile["recent_discoveries"]:
        lines += ["", "RECENTLY DISCOVERED ARTISTS (newest first):"]
        lines += [f"  {d['artist']}" for d in profile["recent_discoveries"][:15]]

    if excluded_titles:
        lines += ["", f"EXCLUDE — already owned, played or dismissed ({len(excluded_titles)} shown):"]
        lines += [f"  {t}" for t in excluded_titles]

    lines += [
        "",
        f"Return exactly {batch_size} recommendations, ordered by match descending.",
    ]
    return "\n".join(lines)


def _excluded_sample(limit: int = 300) -> list[str]:
    """A readable sample of the exclusion set for the prompt."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT artist, title, COUNT(*) AS plays FROM plays "
            "GROUP BY track_key ORDER BY plays DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [f"{row['artist']} — {row['title']}" for row in rows]


# ─── Enrichment ────────────────────────────────────────────────────────────


def enrich(item: dict[str, Any]) -> dict[str, Any]:
    """Resolve one recommendation against MusicBrainz, then Last.fm.

    MusicBrainz is authoritative for names, year and length; Last.fm supplies
    cover art and genre tags. Either can be down or unconfigured — a suggestion
    with no artwork is still a usable suggestion, so failures are absorbed.
    """
    artist = str(item.get("artist", "")).strip()
    title = str(item.get("title", "")).strip()
    album = str(item.get("album", "")).strip()

    enriched = {
        "artist": artist,
        "title": title,
        "album": album,
        "year": "",
        "track_no": 0,
        "duration": 0,
        "recording_mbid": "",
        "cover_url": "",
        "tags": [],
    }

    resolved = musicbrainz.resolve_track(artist, title, album)
    if resolved:
        enriched.update(
            artist=resolved["artist"] or artist,
            title=resolved["title"] or title,
            album=resolved["album"] or album,
            year=resolved["year"],
            track_no=resolved["track"],
            duration=resolved["duration"],
            recording_mbid=resolved["recording_mbid"],
            cover_url=musicbrainz.cover_url(resolved["release_mbid"]),
        )

    if lastfm.configured():
        info = lastfm.track_info(enriched["artist"], enriched["title"])
        if info:
            enriched["tags"] = info["tags"]
            if info["cover_url"]:
                enriched["cover_url"] = info["cover_url"]
            if not enriched["duration"] and info["duration"]:
                enriched["duration"] = info["duration"]
            if not enriched["album"] and info["album"]:
                enriched["album"] = info["album"]
        if not enriched["tags"]:
            enriched["tags"] = lastfm.artist_tags(enriched["artist"])

    return enriched


# ─── The scan ──────────────────────────────────────────────────────────────


def run(trigger: str = "manual") -> dict[str, Any]:
    """Run one full scan. Raises rather than swallowing a configuration error."""
    if not _lock.acquire(blocking=False):
        raise RuntimeError("a scan is already running")

    settings = db.get_settings()
    batch_size = int(settings["batch_size"])
    scan_id = None

    try:
        _state.update(
            running=True, step="starting", done=0, total=batch_size, scan_id=None
        )

        with db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO scans (started_at, trigger, provider, model, requested) "
                "VALUES (?, ?, ?, ?, ?)",
                (db.now(), trigger, ai.provider(), ai.model(), batch_size),
            )
            scan_id = cursor.lastrowid
        _state["scan_id"] = scan_id

        if not ai.available():
            raise RuntimeError(
                f"the {ai.provider()} backend is not configured — set its API key in .env"
            )
        if not history.configured():
            raise RuntimeError(
                "no listening history configured — set LASTFM_USER or "
                "LISTENBRAINZ_USER in .env"
            )

        _state["step"] = "reading scrobbles"
        history.sync()

        if navidrome.configured():
            _state["step"] = "reading your Navidrome hearts"
            history.sync_navidrome()

        _state["step"] = "indexing your library"
        exclude.scan_library()

        _state["step"] = "building your profile"
        profile = history.profile(days=int(settings["history_days"]))
        if profile["plays"] == 0:
            raise RuntimeError(
                f"no plays in the last {settings['history_days']} days — widen the "
                "history window in Settings, or check your scrobble username"
            )

        excluded = exclude.build()

        _state["step"] = f"asking {ai.model()}"
        prompt = build_prompt(profile, _excluded_sample(), batch_size)
        answer = ai.complete_json(
            SYSTEM_PROMPT,
            prompt,
            schema=SCHEMA,
            max_output_tokens=batch_size * TOKENS_PER_RECOMMENDATION,
        )
        items = _recommendations(answer)
        log.info("scan %s: model returned %d recommendations", scan_id, len(items))

        _state.update(step="checking against your library", total=len(items) or batch_size)
        kept = _store(scan_id, items, excluded)

        with db.connect() as conn:
            conn.execute(
                "UPDATE scans SET finished_at = ?, status = 'ok', returned = ?, kept = ? "
                "WHERE id = ?",
                (db.now(), len(items), kept, scan_id),
            )

        _purge(int(settings["retention_days"]))
        log.info("scan %s finished: %d of %d kept", scan_id, kept, len(items))
        return {"scan_id": scan_id, "returned": len(items), "kept": kept}

    except Exception as exc:
        if scan_id is not None:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE scans SET finished_at = ?, status = 'failed', error = ? WHERE id = ?",
                    (db.now(), str(exc)[:500], scan_id),
                )
        log.warning("scan failed: %s", exc)
        raise
    finally:
        _state.update(running=False, step="")
        _lock.release()


def _recommendations(answer: Any) -> list[dict[str, Any]]:
    """Accept the shapes models actually return, not just the one we asked for.

    The schema makes the right shape overwhelmingly likely; it does not make it
    certain, because a backend that cannot enforce a schema falls back to "any
    valid JSON" and a small model given that latitude uses it. Three
    departures are common enough to be worth handling rather than failing on:

    * the list arrives under a wrapper key nobody agreed on ("songs", "data");
    * the list is not a list but an object keyed by ``"Artist — Title"``, with
      the artist and title repeated inside, or not repeated at all;
    * a field is present under another name — ``name`` for ``title``,
      ``score`` for ``match``.

    Anything genuinely unrecoverable is dropped here rather than carried on to
    :func:`_store`, which is where a stray string used to become an
    ``AttributeError`` on ``.get`` and take the scan with it.
    """
    return [item for item in (_normalise(*entry) for entry in _entries(answer)) if item]


_WRAPPER_KEYS = ("recommendations", "tracks", "results", "items", "songs", "suggestions", "data")

# Field names models reach for when they do not use ours.
_ALIASES: dict[str, tuple[str, ...]] = {
    "artist": ("artists", "artist_name", "performer", "band"),
    "title": ("track", "song", "name", "track_title", "song_title"),
    "album": ("release", "album_name"),
    "match": ("score", "confidence", "rating", "match_score", "similarity"),
    "reason": ("why", "rationale", "explanation", "justification", "note"),
    "seed": ("based_on", "seed_artist", "because", "from"),
}

# " — ", " - ", " | " and friends, with the spaces required so that "Jay-Z" and
# "Wu-Tang Clan" are never split down the middle.
_LABEL_SPLIT = re.compile(r"\s+[-—–‒−~|:·/]\s+")


def _entries(answer: Any) -> list[tuple[str, Any]]:
    """``(label, entry)`` pairs, where the label is a key like "Artist — Title"."""
    if isinstance(answer, dict):
        for key in _WRAPPER_KEYS:
            if key in answer:
                return _entries(answer[key])
        # No wrapper key: an object whose own keys name the tracks.
        return [(str(key), value) for key, value in answer.items()]
    if isinstance(answer, list):
        return [("", entry) for entry in answer]
    return []


def _normalise(label: str, entry: Any) -> dict[str, Any] | None:
    """One recommendation in our field names, or ``None`` if it is not one."""
    if isinstance(entry, dict):
        item = dict(entry)
    elif isinstance(entry, str):
        # A bare "Artist — Title" string, with nothing else to go on.
        item, label = {}, label or entry
    else:
        return None  # a number, a null, a nested list — nothing to recover

    for field, aliases in _ALIASES.items():
        if _text(item.get(field)):
            continue
        for alias in aliases:
            if _text(item.get(alias)):
                item[field] = item[alias]
                break

    # Fall back to the key the entry was filed under. Only ever fills a gap:
    # what the model put *inside* the object is better evidence than how it
    # chose to label it.
    if label and not (_text(item.get("artist")) and _text(item.get("title"))):
        artist, title = _split_label(label)
        if artist and not _text(item.get("artist")):
            item["artist"] = artist
        if title and not _text(item.get("title")):
            item["title"] = title

    for field in ("artist", "title", "album", "reason", "seed"):
        if field in item:
            item[field] = _text(item[field])

    return item or None


def _text(value: Any) -> str:
    """A field as a string. Lists happen — "artists" is regularly one."""
    if value is None or isinstance(value, (dict, bool)):
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(part for part in (_text(v) for v in value) if part)
    return str(value).strip()


def _split_label(label: str) -> tuple[str, str]:
    """``"Gareth Emery — Laserface 01"`` into its artist and its title."""
    parts = _LABEL_SPLIT.split(label.strip(), maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", label.strip()


def _match_percent(value: Any) -> int:
    """A confidence as 0-100, however the model chose to express it.

    Asked for an integer percentage, models still answer "87%", or 0.87 — and
    a fraction read as an integer is a card that sorts to the bottom and never
    auto-downloads. How it was written is the evidence for what it means: a
    bare ``1`` is one percent, while ``1.0`` was written on a 0-1 scale and
    means all of it.
    """
    if isinstance(value, bool) or value is None:
        return 0
    raw = str(value).strip().rstrip("%").strip()
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return 0
    if 0 < number <= 1 and ("." in raw or isinstance(value, float)):
        number *= 100
    return max(0, min(100, int(number)))


def _store(scan_id: int, items: list[dict[str, Any]], excluded: set[str]) -> int:
    """Enrich and insert, skipping anything excluded before or after enrichment.

    The hearts are applied here rather than earlier because the boost is scored
    against the *enriched* artist and genre tags. The model's spelling of an
    artist is whatever it remembered; MusicBrainz's is the one that will match
    what Navidrome has on disk, and matching those two strings is the entire
    mechanism.
    """
    kept = 0
    seen: set[str] = set()
    per_artist: dict[str, int] = {}
    # Read once. A scan scores forty of these against tables nothing else is
    # writing to while it runs.
    hearts = affinity.load()

    for index, item in enumerate(items):
        _state["done"] = index
        artist = str(item.get("artist", "")).strip()
        title = str(item.get("title", "")).strip()
        if not artist or not title:
            continue

        key = track_key(artist, title)
        if key in excluded or key in seen:
            continue

        # The prompt asks for at most two per artist; enforce it rather than trust it.
        akey = artist_key(artist)
        if per_artist.get(akey, 0) >= 2:
            continue

        enriched = enrich(item)
        canonical = track_key(enriched["artist"], enriched["title"])
        if canonical in excluded or (canonical != key and canonical in seen):
            continue

        seen.update({key, canonical})
        per_artist[akey] = per_artist.get(akey, 0) + 1

        seed = str(item.get("seed", ""))[:120]
        scored = affinity.apply(
            _match_percent(item.get("match")),
            enriched["artist"],
            seed=seed,
            tags=enriched["tags"],
            picture=hearts,
        )

        with db.connect() as conn:
            conn.execute(
                "INSERT INTO suggestions (scan_id, track_key, artist, title, album, year, "
                "track_no, match, match_base, affinity, affinity_reason, reason, seed, tags, "
                "cover_url, duration, recording_mbid, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                # A re-suggested track refreshes the card it already has rather
                # than making a second one, and only while it is still 'new' —
                # so something you hid can never come back.
                "ON CONFLICT (track_key) DO UPDATE SET "
                "  scan_id = excluded.scan_id, match = excluded.match, "
                "  match_base = excluded.match_base, affinity = excluded.affinity, "
                "  affinity_reason = excluded.affinity_reason, "
                "  reason = excluded.reason, seed = excluded.seed, "
                "  created_at = excluded.created_at "
                "WHERE suggestions.status = 'new'",
                (
                    scan_id, canonical, enriched["artist"], enriched["title"],
                    enriched["album"], enriched["year"], enriched["track_no"],
                    scored["match"], scored["match_base"], scored["affinity"],
                    scored["affinity_reason"],
                    str(item.get("reason", ""))[:400], seed,
                    ",".join(enriched["tags"]), enriched["cover_url"],
                    enriched["duration"], enriched["recording_mbid"], db.now(),
                ),
            )
        kept += 1

    _state["done"] = len(items)
    return kept


def _purge(retention_days: int) -> None:
    """Drop un-actioned suggestions past the retention window.

    Saved, hidden and downloaded cards are kept: they are decisions, and a
    decision you made is worth more than the disk row it costs.
    """
    cutoff = db.now() - retention_days * 86400
    with db.connect() as conn:
        conn.execute("DELETE FROM suggestions WHERE status = 'new' AND created_at < ?", (cutoff,))


def run_in_background(trigger: str = "manual") -> bool:
    """Start a scan on its own thread. False if one is already running."""
    if _state["running"]:
        return False

    def target() -> None:
        from . import download

        try:
            result = run(trigger)
        except Exception:
            return  # already logged, and recorded on the scan row
        download.auto_enqueue(result["scan_id"])

    threading.Thread(target=target, name="musicdrome-scan", daemon=True).start()
    return True
