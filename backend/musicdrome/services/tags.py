"""Audio tag extraction.

``mutagen`` exposes a different tag object per container (ID3 frames, Vorbis
comments, MP4 atoms, ASF attributes, APEv2 keys). This module flattens all of
them into one canonical :class:`TrackTags` so the scanner never has to care what
it just opened.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import mutagen
from mutagen.apev2 import APEv2
from mutagen.asf import ASFTags
from mutagen.id3 import ID3
from mutagen.mp4 import MP4Tags

from ..config import settings

log = logging.getLogger(__name__)

# ─── Canonical field → per-format source keys ──────────────────────────────

ID3_MAP = {
    "title": ["TIT2"],
    "artist": ["TPE1"],
    "album_artist": ["TPE2"],
    "album": ["TALB"],
    "genre": ["TCON"],
    "date": ["TDRC", "TYER", "TDRL", "TDOR"],
    "track_number": ["TRCK"],
    "disc_number": ["TPOS"],
    "composer": ["TCOM"],
    "bpm": ["TBPM"],
    "compilation": ["TCMP"],
    "sort_title": ["TSOT"],
    "sort_artist": ["TSOP"],
    "sort_album": ["TSOA"],
}

# ID3 user-defined text frames (TXXX:<desc>)
ID3_TXXX_MAP = {
    "mb_recording_id": ["MusicBrainz Release Track Id", "MusicBrainz Track Id"],
    "mb_release_id": ["MusicBrainz Album Id"],
    "mb_artist_id": ["MusicBrainz Artist Id"],
    "mb_release_group_id": ["MusicBrainz Release Group Id"],
    "mb_album_artist_id": ["MusicBrainz Album Artist Id"],
}

VORBIS_MAP = {
    "title": ["title"],
    "artist": ["artist"],
    "album_artist": ["albumartist", "album artist"],
    "album": ["album"],
    "genre": ["genre"],
    "date": ["date", "year", "originaldate"],
    "track_number": ["tracknumber", "track"],
    "disc_number": ["discnumber", "disc"],
    "composer": ["composer"],
    "bpm": ["bpm"],
    "compilation": ["compilation"],
    "comment": ["comment", "description"],
    "lyrics": ["lyrics", "unsyncedlyrics"],
    "sort_title": ["titlesort"],
    "sort_artist": ["artistsort"],
    "sort_album": ["albumsort"],
    "mb_recording_id": ["musicbrainz_trackid", "musicbrainz_recordingid"],
    "mb_release_id": ["musicbrainz_albumid"],
    "mb_artist_id": ["musicbrainz_artistid"],
    "mb_release_group_id": ["musicbrainz_releasegroupid"],
    "mb_album_artist_id": ["musicbrainz_albumartistid"],
}

MP4_MAP = {
    "title": ["\xa9nam"],
    "artist": ["\xa9ART"],
    "album_artist": ["aART"],
    "album": ["\xa9alb"],
    "genre": ["\xa9gen"],
    "date": ["\xa9day"],
    "composer": ["\xa9wrt"],
    "comment": ["\xa9cmt"],
    "lyrics": ["\xa9lyr"],
    "bpm": ["tmpo"],
    "compilation": ["cpil"],
    "sort_title": ["sonm"],
    "sort_artist": ["soar"],
    "sort_album": ["soal"],
}

MP4_FREEFORM_MAP = {
    "mb_recording_id": ["MusicBrainz Track Id"],
    "mb_release_id": ["MusicBrainz Album Id"],
    "mb_artist_id": ["MusicBrainz Artist Id"],
    "mb_release_group_id": ["MusicBrainz Release Group Id"],
}

ASF_MAP = {
    "title": ["Title"],
    "artist": ["Author", "WM/AlbumArtist"],
    "album_artist": ["WM/AlbumArtist"],
    "album": ["WM/AlbumTitle"],
    "genre": ["WM/Genre"],
    "date": ["WM/Year"],
    "track_number": ["WM/TrackNumber"],
    "disc_number": ["WM/PartOfSet"],
    "composer": ["WM/Composer"],
    "comment": ["Description"],
    "mb_recording_id": ["MusicBrainz/Track Id"],
    "mb_release_id": ["MusicBrainz/Album Id"],
    "mb_artist_id": ["MusicBrainz/Artist Id"],
}

APE_MAP = {
    "title": ["Title"],
    "artist": ["Artist"],
    "album_artist": ["Album Artist", "AlbumArtist"],
    "album": ["Album"],
    "genre": ["Genre"],
    "date": ["Year", "Date"],
    "track_number": ["Track"],
    "disc_number": ["Disc"],
    "composer": ["Composer"],
    "comment": ["Comment"],
}

CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "opus": "audio/opus",
    "m4a": "audio/mp4",
    "m4b": "audio/mp4",
    "aac": "audio/aac",
    "wav": "audio/wav",
    "wma": "audio/x-ms-wma",
    "aiff": "audio/aiff",
    "aif": "audio/aiff",
    "ape": "audio/x-monkeys-audio",
    "mpc": "audio/x-musepack",
    "wv": "audio/x-wavpack",
}

_YEAR_RE = re.compile(r"(\d{4})")
_NUM_RE = re.compile(r"^\s*(\d+)")


@dataclass
class TrackTags:
    title: str = ""
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    genre: str = ""
    genres: list[str] = field(default_factory=list)
    year: int | None = None
    release_date: str = ""
    track_number: int = 0
    disc_number: int = 1
    composer: str = ""
    comment: str = ""
    lyrics: str = ""
    bpm: int | None = None
    compilation: bool = False

    sort_title: str = ""
    sort_artist: str = ""
    sort_album: str = ""

    mb_recording_id: str = ""
    mb_release_id: str = ""
    mb_artist_id: str = ""
    mb_release_group_id: str = ""
    mb_album_artist_id: str = ""

    duration: int = 0
    bitrate: int = 0
    sample_rate: int = 0
    channels: int = 2
    size: int = 0
    suffix: str = ""
    content_type: str = "audio/mpeg"
    has_embedded_art: bool = False


# ─── Value coercion ────────────────────────────────────────────────────────


def _first(value) -> str:
    """Normalise any mutagen value shape (list, frame, atom) to a string."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        value = value[0]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value).strip()
    return text.replace("\x00", "")


def _to_int(value) -> int:
    """Parse ``"3"``, ``"3/12"`` or ``3`` into an int; 0 when unparseable."""
    text = _first(value)
    if not text:
        return 0
    match = _NUM_RE.match(text)
    return int(match.group(1)) if match else 0


def _to_year(value) -> int | None:
    text = _first(value)
    if not text:
        return None
    match = _YEAR_RE.search(text)
    if not match:
        return None
    year = int(match.group(1))
    return year if 1000 <= year <= 2999 else None


def _to_bool(value) -> bool:
    return _first(value).lower() in {"1", "true", "yes", "on"}


def split_multivalue(raw: str) -> list[str]:
    """Split a multi-artist / multi-genre tag on the configured separators."""
    if not raw:
        return []
    separators = [s for s in settings.multivalue_separators.split(",") if s]
    parts = [raw]
    for sep in separators:
        expanded: list[str] = []
        for part in parts:
            expanded.extend(part.split(sep))
        parts = expanded
    return [p.strip() for p in parts if p.strip()]


# ─── Per-container extraction ──────────────────────────────────────────────


def _extract_id3(tags: ID3, out: dict) -> None:
    for field_name, frame_ids in ID3_MAP.items():
        for frame_id in frame_ids:
            frame = tags.get(frame_id)
            if frame is not None:
                text = _first(getattr(frame, "text", frame))
                if text:
                    out[field_name] = text
                    break

    for key, frame in tags.items():
        if not key.startswith("TXXX:"):
            continue
        desc = key[5:]
        for field_name, descriptions in ID3_TXXX_MAP.items():
            if any(desc.lower() == d.lower() for d in descriptions):
                value = _first(getattr(frame, "text", frame))
                if value and not out.get(field_name):
                    out[field_name] = value

    comm = [v for k, v in tags.items() if k.startswith("COMM")]
    if comm:
        out.setdefault("comment", _first(getattr(comm[0], "text", comm[0])))
    uslt = [v for k, v in tags.items() if k.startswith("USLT")]
    if uslt:
        out.setdefault("lyrics", _first(getattr(uslt[0], "text", uslt[0])))

    out["has_embedded_art"] = any(k.startswith("APIC") for k in tags.keys())


def _extract_vorbis(tags, out: dict) -> None:
    lowered = {k.lower(): v for k, v in tags.items()}
    for field_name, keys in VORBIS_MAP.items():
        for key in keys:
            if key in lowered:
                value = _first(lowered[key])
                if value:
                    out[field_name] = value
                    break
    # Vorbis genre/artist can legitimately repeat rather than use separators
    if "genre" in lowered and isinstance(lowered["genre"], list) and len(lowered["genre"]) > 1:
        out["genres"] = [str(g).strip() for g in lowered["genre"] if str(g).strip()]


def _extract_mp4(tags: MP4Tags, out: dict) -> None:
    for field_name, atoms in MP4_MAP.items():
        for atom in atoms:
            if atom in tags:
                value = tags[atom]
                out[field_name] = _first(value)
                break
    for atom, value in tags.items():
        if not atom.startswith("----:"):
            continue
        desc = atom.rsplit(":", 1)[-1]
        for field_name, descriptions in MP4_FREEFORM_MAP.items():
            if any(desc.lower() == d.lower() for d in descriptions):
                out.setdefault(field_name, _first(value))

    if "trkn" in tags and tags["trkn"]:
        pair = tags["trkn"][0]
        out["track_number"] = str(pair[0]) if pair else "0"
    if "disk" in tags and tags["disk"]:
        pair = tags["disk"][0]
        out["disc_number"] = str(pair[0]) if pair else "1"
    out["has_embedded_art"] = bool(tags.get("covr"))


def _extract_asf(tags: ASFTags, out: dict) -> None:
    for field_name, keys in ASF_MAP.items():
        for key in keys:
            if key in tags:
                value = _first(tags[key])
                if value:
                    out[field_name] = value
                    break


def _extract_ape(tags: APEv2, out: dict) -> None:
    lowered = {k.lower(): v for k, v in tags.items()}
    for field_name, keys in APE_MAP.items():
        for key in keys:
            if key.lower() in lowered:
                value = _first(lowered[key.lower()])
                if value:
                    out[field_name] = value
                    break


# ─── Public API ────────────────────────────────────────────────────────────


def read_tags(path: Path) -> TrackTags | None:
    """Read one audio file. Returns ``None`` if mutagen cannot open it."""
    try:
        audio = mutagen.File(path)
    except Exception as exc:  # mutagen raises a wide variety on damaged files
        log.warning("cannot read tags from %s: %s", path, exc)
        return None

    if audio is None:
        log.debug("unrecognised audio format: %s", path)
        return None

    raw: dict = {}
    tags = getattr(audio, "tags", None)
    if tags is not None:
        try:
            if isinstance(tags, ID3):
                _extract_id3(tags, raw)
            elif isinstance(tags, MP4Tags):
                _extract_mp4(tags, raw)
            elif isinstance(tags, ASFTags):
                _extract_asf(tags, raw)
            elif isinstance(tags, APEv2):
                _extract_ape(tags, raw)
            else:
                # FLAC / Ogg / Opus all present a Vorbis-comment mapping
                _extract_vorbis(tags, raw)
        except Exception as exc:
            log.warning("tag extraction failed for %s: %s", path, exc)

    # FLAC and Ogg carry pictures outside the comment block
    if not raw.get("has_embedded_art"):
        raw["has_embedded_art"] = bool(getattr(audio, "pictures", None))

    info = getattr(audio, "info", None)
    try:
        stat = path.stat()
        size = stat.st_size
    except OSError:
        size = 0

    suffix = path.suffix.lower().lstrip(".")
    genre_raw = raw.get("genre", "")
    genres = raw.get("genres") or split_multivalue(genre_raw)

    artist = raw.get("artist", "") or raw.get("album_artist", "")
    album_artist = raw.get("album_artist", "") or artist

    result = TrackTags(
        title=raw.get("title", "") or path.stem,
        artist=artist or "Unknown Artist",
        album_artist=album_artist or "Unknown Artist",
        album=raw.get("album", "") or "Unknown Album",
        genre=genres[0] if genres else "",
        genres=genres,
        year=_to_year(raw.get("date", "")),
        release_date=raw.get("date", ""),
        track_number=_to_int(raw.get("track_number", 0)),
        disc_number=_to_int(raw.get("disc_number", 1)) or 1,
        composer=raw.get("composer", ""),
        comment=raw.get("comment", ""),
        lyrics=raw.get("lyrics", ""),
        bpm=_to_int(raw.get("bpm", 0)) or None,
        compilation=_to_bool(raw.get("compilation", "")),
        sort_title=raw.get("sort_title", ""),
        sort_artist=raw.get("sort_artist", ""),
        sort_album=raw.get("sort_album", ""),
        mb_recording_id=raw.get("mb_recording_id", ""),
        mb_release_id=raw.get("mb_release_id", ""),
        mb_artist_id=raw.get("mb_artist_id", ""),
        mb_release_group_id=raw.get("mb_release_group_id", ""),
        mb_album_artist_id=raw.get("mb_album_artist_id", ""),
        duration=int(getattr(info, "length", 0) or 0),
        bitrate=int((getattr(info, "bitrate", 0) or 0) / 1000),
        sample_rate=int(getattr(info, "sample_rate", 0) or 0),
        channels=int(getattr(info, "channels", 2) or 2),
        size=size,
        suffix=suffix,
        content_type=CONTENT_TYPES.get(suffix, "audio/mpeg"),
        has_embedded_art=bool(raw.get("has_embedded_art")),
    )

    # Lossless formats often report bitrate 0; derive it from size/duration.
    if result.bitrate == 0 and result.duration > 0 and size > 0:
        result.bitrate = int((size * 8) / result.duration / 1000)

    return result


def extract_embedded_art(path: Path) -> tuple[bytes, str] | None:
    """Return ``(image_bytes, mime)`` for embedded cover art, if any."""
    try:
        audio = mutagen.File(path)
    except Exception:
        return None
    if audio is None:
        return None

    pictures = getattr(audio, "pictures", None)
    if pictures:
        pic = pictures[0]
        return pic.data, getattr(pic, "mime", "image/jpeg")

    tags = getattr(audio, "tags", None)
    if tags is None:
        return None

    if isinstance(tags, ID3):
        for key in tags.keys():
            if key.startswith("APIC"):
                frame = tags[key]
                return frame.data, getattr(frame, "mime", "image/jpeg")
    elif isinstance(tags, MP4Tags):
        covers = tags.get("covr")
        if covers:
            from mutagen.mp4 import MP4Cover

            cover = covers[0]
            is_png = getattr(cover, "imageformat", None) == MP4Cover.FORMAT_PNG
            return bytes(cover), "image/png" if is_png else "image/jpeg"
    else:
        # Vorbis comments store art base64-encoded under metadata_block_picture
        data = tags.get("metadata_block_picture")
        if data:
            import base64

            from mutagen.flac import Picture

            try:
                pic = Picture(base64.b64decode(_first(data)))
                return pic.data, pic.mime or "image/jpeg"
            except Exception:
                return None
    return None
