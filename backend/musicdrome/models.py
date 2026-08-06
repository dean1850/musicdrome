"""SQLAlchemy models.

Layout notes:

* Per-user state (stars, ratings, play counts) lives in :class:`Annotation`
  keyed by ``(user_id, item_type, item_id)`` rather than on the media rows, so
  a shared library stays shared while every user keeps their own history.
* Media rows carry both structured relations and a denormalised display string
  (``artist_name``, ``genre``) — the Subsonic API asks for those on nearly every
  response and the join is not worth paying repeatedly.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

# ─── Association tables ────────────────────────────────────────────────────

track_genres = Table(
    "track_genres",
    Base.metadata,
    Column("track_id", ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True),
    Column("genre_id", ForeignKey("genres.id", ondelete="CASCADE"), primary_key=True),
)

album_genres = Table(
    "album_genres",
    Base.metadata,
    Column("album_id", ForeignKey("albums.id", ondelete="CASCADE"), primary_key=True),
    Column("genre_id", ForeignKey("genres.id", ondelete="CASCADE"), primary_key=True),
)

artist_genres = Table(
    "artist_genres",
    Base.metadata,
    Column("artist_id", ForeignKey("artists.id", ondelete="CASCADE"), primary_key=True),
    Column("genre_id", ForeignKey("genres.id", ondelete="CASCADE"), primary_key=True),
)


# ─── Enums ─────────────────────────────────────────────────────────────────


class ItemType(str, enum.Enum):
    ARTIST = "artist"
    ALBUM = "album"
    TRACK = "track"


class WantedStatus(str, enum.Enum):
    PENDING = "pending"       # awaiting user approval
    APPROVED = "approved"     # approved, queued for acquisition
    DOWNLOADING = "downloading"
    IMPORTED = "imported"
    REJECTED = "rejected"
    FAILED = "failed"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScrobbleStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


# ─── Users ─────────────────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), default=None)

    # argon2 hash for web login
    password_hash: Mapped[str] = mapped_column(String(255))
    # Fernet-encrypted copy — Subsonic's token auth is md5(password + salt) and
    # cannot be satisfied by a one-way hash. Navidrome makes the same trade.
    password_enc: Mapped[str | None] = mapped_column(Text, default=None)

    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Subsonic per-user permission flags
    download_role: Mapped[bool] = mapped_column(Boolean, default=True)
    upload_role: Mapped[bool] = mapped_column(Boolean, default=False)
    playlist_role: Mapped[bool] = mapped_column(Boolean, default=True)
    cover_art_role: Mapped[bool] = mapped_column(Boolean, default=True)
    comment_role: Mapped[bool] = mapped_column(Boolean, default=False)
    podcast_role: Mapped[bool] = mapped_column(Boolean, default=True)
    stream_role: Mapped[bool] = mapped_column(Boolean, default=True)
    jukebox_role: Mapped[bool] = mapped_column(Boolean, default=False)
    share_role: Mapped[bool] = mapped_column(Boolean, default=False)

    # Playback preferences
    max_bitrate: Mapped[int] = mapped_column(Integer, default=0)  # 0 = server default
    transcode_format: Mapped[str | None] = mapped_column(String(16), default=None)

    # Third-party scrobbling — tokens stored Fernet-encrypted
    lastfm_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    lastfm_username: Mapped[str | None] = mapped_column(String(255), default=None)
    lastfm_session_key: Mapped[str | None] = mapped_column(Text, default=None)
    listenbrainz_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    listenbrainz_token: Mapped[str | None] = mapped_column(Text, default=None)

    # AI features opt-in
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    playlists: Mapped[list[Playlist]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )


# ─── Library: artists / albums / tracks ────────────────────────────────────


class Genre(Base):
    __tablename__ = "genres"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(500), index=True)
    sort_name: Mapped[str] = mapped_column(String(500), index=True, default="")
    mbid: Mapped[str | None] = mapped_column(String(36), index=True, default=None)

    biography: Mapped[str | None] = mapped_column(Text, default=None)
    image_path: Mapped[str | None] = mapped_column(Text, default=None)
    image_url: Mapped[str | None] = mapped_column(Text, default=None)
    lastfm_url: Mapped[str | None] = mapped_column(Text, default=None)
    listener_count: Mapped[int] = mapped_column(Integer, default=0)
    global_play_count: Mapped[int] = mapped_column(Integer, default=0)

    album_count: Mapped[int] = mapped_column(Integer, default=0)
    track_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    albums: Mapped[list[Album]] = relationship(
        back_populates="artist", cascade="all, delete-orphan"
    )
    genres: Mapped[list[Genre]] = relationship(secondary=artist_genres)

    __table_args__ = (UniqueConstraint("name", name="uq_artist_name"),)


class Album(Base):
    __tablename__ = "albums"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(500), index=True)
    sort_name: Mapped[str] = mapped_column(String(500), index=True, default="")
    artist_id: Mapped[int | None] = mapped_column(
        ForeignKey("artists.id", ondelete="CASCADE"), index=True, default=None
    )
    artist_name: Mapped[str] = mapped_column(String(500), default="")
    album_artist: Mapped[str] = mapped_column(String(500), default="")

    mbid: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    mb_release_group_id: Mapped[str | None] = mapped_column(String(36), default=None)

    year: Mapped[int | None] = mapped_column(Integer, index=True, default=None)
    release_date: Mapped[str | None] = mapped_column(String(20), default=None)
    genre: Mapped[str] = mapped_column(String(255), default="", index=True)
    compilation: Mapped[bool] = mapped_column(Boolean, default=False)

    cover_art_path: Mapped[str | None] = mapped_column(Text, default=None)
    duration: Mapped[int] = mapped_column(Integer, default=0)
    song_count: Mapped[int] = mapped_column(Integer, default=0)
    size: Mapped[int] = mapped_column(Integer, default=0)
    disc_count: Mapped[int] = mapped_column(Integer, default=1)

    folder_path: Mapped[str | None] = mapped_column(Text, default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    artist: Mapped[Artist | None] = relationship(back_populates="albums")
    tracks: Mapped[list[Track]] = relationship(
        back_populates="album", cascade="all, delete-orphan"
    )
    genres: Mapped[list[Genre]] = relationship(secondary=album_genres)

    __table_args__ = (
        Index("ix_album_artist_name", "artist_id", "name"),
        Index("ix_album_created", "created_at"),
    )


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(Text, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500), index=True)
    sort_title: Mapped[str] = mapped_column(String(500), default="")

    album_id: Mapped[int | None] = mapped_column(
        ForeignKey("albums.id", ondelete="CASCADE"), index=True, default=None
    )
    artist_id: Mapped[int | None] = mapped_column(
        ForeignKey("artists.id", ondelete="CASCADE"), index=True, default=None
    )
    artist_name: Mapped[str] = mapped_column(String(500), default="", index=True)
    album_name: Mapped[str] = mapped_column(String(500), default="")
    album_artist: Mapped[str] = mapped_column(String(500), default="")

    track_number: Mapped[int] = mapped_column(Integer, default=0)
    disc_number: Mapped[int] = mapped_column(Integer, default=1)
    year: Mapped[int | None] = mapped_column(Integer, index=True, default=None)
    genre: Mapped[str] = mapped_column(String(255), default="", index=True)

    duration: Mapped[int] = mapped_column(Integer, default=0)  # seconds
    bitrate: Mapped[int] = mapped_column(Integer, default=0)   # kbps
    sample_rate: Mapped[int] = mapped_column(Integer, default=0)
    channels: Mapped[int] = mapped_column(Integer, default=2)
    size: Mapped[int] = mapped_column(Integer, default=0)
    suffix: Mapped[str] = mapped_column(String(16), default="")
    content_type: Mapped[str] = mapped_column(String(64), default="audio/mpeg")
    bpm: Mapped[int | None] = mapped_column(Integer, default=None)

    mbid: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    mb_release_id: Mapped[str | None] = mapped_column(String(36), default=None)
    mb_artist_id: Mapped[str | None] = mapped_column(String(36), default=None)

    has_cover_art: Mapped[bool] = mapped_column(Boolean, default=False)
    cover_art_path: Mapped[str | None] = mapped_column(Text, default=None)
    lyrics: Mapped[str | None] = mapped_column(Text, default=None)
    comment: Mapped[str | None] = mapped_column(Text, default=None)

    mtime: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    album: Mapped[Album | None] = relationship(back_populates="tracks")
    artist: Mapped[Artist | None] = relationship()
    genres: Mapped[list[Genre]] = relationship(secondary=track_genres)

    __table_args__ = (
        Index("ix_track_album_disc_num", "album_id", "disc_number", "track_number"),
        Index("ix_track_search", "title", "artist_name"),
    )


# ─── Per-user annotations ──────────────────────────────────────────────────


class Annotation(Base):
    """Star / rating / play-count state, one row per user per item."""

    __tablename__ = "annotations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    item_type: Mapped[str] = mapped_column(String(16), index=True)
    item_id: Mapped[int] = mapped_column(Integer, index=True)

    starred_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    rating: Mapped[int] = mapped_column(Integer, default=0)  # 0-5
    play_count: Mapped[int] = mapped_column(Integer, default=0)
    play_date: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    __table_args__ = (
        UniqueConstraint("user_id", "item_type", "item_id", name="uq_annotation"),
        Index("ix_annotation_lookup", "user_id", "item_type", "item_id"),
    )


class PlayHistory(Base):
    """Append-only listening log — the substrate for analytics and AI profiling."""

    __tablename__ = "play_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    track_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL"), index=True, default=None
    )
    # Denormalised so history survives the track being removed from disk
    title: Mapped[str] = mapped_column(String(500), default="")
    artist_name: Mapped[str] = mapped_column(String(500), default="", index=True)
    album_name: Mapped[str] = mapped_column(String(500), default="")
    genre: Mapped[str] = mapped_column(String(255), default="")
    duration: Mapped[int] = mapped_column(Integer, default=0)

    played_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    client: Mapped[str] = mapped_column(String(120), default="")
    source: Mapped[str] = mapped_column(String(32), default="stream")

    __table_args__ = (Index("ix_history_user_time", "user_id", "played_at"),)


class ScrobbleQueue(Base):
    """Outbound scrobbles with retry state, one row per (play, service)."""

    __tablename__ = "scrobble_queue"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    track_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL"), default=None
    )
    service: Mapped[str] = mapped_column(String(32), index=True)  # lastfm | listenbrainz

    title: Mapped[str] = mapped_column(String(500), default="")
    artist_name: Mapped[str] = mapped_column(String(500), default="")
    album_name: Mapped[str] = mapped_column(String(500), default="")
    album_artist: Mapped[str] = mapped_column(String(500), default="")
    track_number: Mapped[int] = mapped_column(Integer, default=0)
    duration: Mapped[int] = mapped_column(Integer, default=0)
    mbid: Mapped[str | None] = mapped_column(String(36), default=None)

    played_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(16), default=ScrobbleStatus.PENDING.value,
                                        index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ─── Playlists ─────────────────────────────────────────────────────────────


class Playlist(Base):
    __tablename__ = "playlists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(500), index=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    public: Mapped[bool] = mapped_column(Boolean, default=False)

    # Smart playlists hold a rule document instead of a fixed track list; the
    # entries are recomputed on a schedule.
    is_smart: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    rules: Mapped[dict | None] = mapped_column(JSON, default=None)

    # AI-curated playlists additionally record the prompt and the model's
    # rationale so the UI can explain why a track is there.
    is_ai: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    ai_prompt: Mapped[str | None] = mapped_column(Text, default=None)
    ai_rationale: Mapped[str | None] = mapped_column(Text, default=None)
    ai_seed: Mapped[dict | None] = mapped_column(JSON, default=None)
    last_generated_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    # Playlists read from an .m3u on disk stay bound to that file: its mtime
    # drives a re-import and deleting it deletes the playlist. ``sync`` goes
    # false once the track list is edited by hand, which hands ownership to the
    # user without losing ``import_path`` — otherwise the next pass would see an
    # unknown file and import a duplicate.
    is_imported: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    import_path: Mapped[str | None] = mapped_column(Text, default=None, index=True)
    import_mtime: Mapped[float] = mapped_column(Float, default=0.0)
    import_missing: Mapped[int] = mapped_column(Integer, default=0)
    sync: Mapped[bool] = mapped_column(Boolean, default=False)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    cover_art_path: Mapped[str | None] = mapped_column(Text, default=None)
    duration: Mapped[int] = mapped_column(Integer, default=0)
    song_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    owner: Mapped[User] = relationship(back_populates="playlists")
    entries: Mapped[list[PlaylistTrack]] = relationship(
        back_populates="playlist",
        cascade="all, delete-orphan",
        order_by="PlaylistTrack.position",
    )


class PlaylistTrack(Base):
    __tablename__ = "playlist_tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[int] = mapped_column(
        ForeignKey("playlists.id", ondelete="CASCADE"), index=True
    )
    track_id: Mapped[int] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    # Why the AI picked this track, when applicable
    note: Mapped[str | None] = mapped_column(Text, default=None)

    playlist: Mapped[Playlist] = relationship(back_populates="entries")
    track: Mapped[Track] = relationship()

    __table_args__ = (Index("ix_playlist_position", "playlist_id", "position"),)


class PlayQueue(Base):
    """Subsonic savePlayQueue/getPlayQueue state — one per user."""

    __tablename__ = "play_queues"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    track_ids: Mapped[list] = mapped_column(JSON, default=list)
    current_track_id: Mapped[int | None] = mapped_column(Integer, default=None)
    position_ms: Mapped[int] = mapped_column(Integer, default=0)
    changed_by: Mapped[str] = mapped_column(String(120), default="")
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Bookmark(Base):
    __tablename__ = "bookmarks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    position_ms: Mapped[int] = mapped_column(Integer, default=0)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    track: Mapped[Track] = relationship()

    __table_args__ = (UniqueConstraint("user_id", "track_id", name="uq_bookmark"),)


# ─── Podcasts ──────────────────────────────────────────────────────────────


class PodcastChannel(Base):
    __tablename__ = "podcast_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(Text, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(500), default="")
    link: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str | None] = mapped_column(Text, default=None)
    image_path: Mapped[str | None] = mapped_column(Text, default=None)
    categories: Mapped[list | None] = mapped_column(JSON, default=None)
    language: Mapped[str] = mapped_column(String(16), default="")

    status: Mapped[str] = mapped_column(String(32), default="new")  # new|downloading|completed|error
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    auto_download: Mapped[bool] = mapped_column(Boolean, default=False)

    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    episodes: Mapped[list[PodcastEpisode]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )


class PodcastEpisode(Base):
    __tablename__ = "podcast_episodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("podcast_channels.id", ondelete="CASCADE"), index=True
    )
    guid: Mapped[str] = mapped_column(String(500), index=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    publish_date: Mapped[datetime | None] = mapped_column(DateTime, index=True, default=None)
    duration: Mapped[int] = mapped_column(Integer, default=0)
    size: Mapped[int] = mapped_column(Integer, default=0)
    bitrate: Mapped[int] = mapped_column(Integer, default=0)
    suffix: Mapped[str] = mapped_column(String(16), default="mp3")
    content_type: Mapped[str] = mapped_column(String(64), default="audio/mpeg")

    stream_url: Mapped[str] = mapped_column(Text, default="")
    path: Mapped[str | None] = mapped_column(Text, default=None)
    # new | downloading | completed | error | deleted | skipped
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    channel: Mapped[PodcastChannel] = relationship(back_populates="episodes")

    __table_args__ = (UniqueConstraint("channel_id", "guid", name="uq_episode_guid"),)


# ─── Discovery: recommendations, wanted items, acquisition jobs ────────────


class Recommendation(Base):
    """A suggested artist/album/track not yet in the library."""

    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    item_type: Mapped[str] = mapped_column(String(16), default="track")
    artist_name: Mapped[str] = mapped_column(String(500), default="", index=True)
    album_name: Mapped[str] = mapped_column(String(500), default="")
    title: Mapped[str] = mapped_column(String(500), default="")
    mbid: Mapped[str | None] = mapped_column(String(36), default=None)

    source: Mapped[str] = mapped_column(String(32), default="lastfm", index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    seed_artist: Mapped[str] = mapped_column(String(500), default="")

    in_library: Mapped[bool] = mapped_column(Boolean, default=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    __table_args__ = (
        Index("ix_reco_user_dismissed", "user_id", "dismissed", "score"),
    )


class WantedItem(Base):
    """Approval queue between a recommendation and an actual download."""

    __tablename__ = "wanted_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, default=None
    )
    item_type: Mapped[str] = mapped_column(String(16), default="track")
    artist_name: Mapped[str] = mapped_column(String(500), default="")
    album_name: Mapped[str] = mapped_column(String(500), default="")
    title: Mapped[str] = mapped_column(String(500), default="")
    mbid: Mapped[str | None] = mapped_column(String(36), default=None)

    source: Mapped[str] = mapped_column(String(32), default="ai")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")

    # ytdlp | lidarr — which acquisition backend should service this
    provider: Mapped[str] = mapped_column(String(32), default="ytdlp", index=True)
    status: Mapped[str] = mapped_column(
        String(24), default=WantedStatus.PENDING.value, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    result_path: Mapped[str | None] = mapped_column(Text, default=None)
    track_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL"), default=None
    )
    external_id: Mapped[str | None] = mapped_column(String(120), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    __table_args__ = (
        Index("ix_wanted_status_created", "status", "created_at"),
    )


class Job(Base):
    """Persistent background work — survives a restart, unlike an in-memory queue."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED.value, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, default=None)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    __table_args__ = (Index("ix_job_status_priority", "status", "priority", "created_at"),)


# ─── AI outputs ────────────────────────────────────────────────────────────


class AIReport(Base):
    """Cached model output: taste profiles, listening reports, insights."""

    __tablename__ = "ai_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(48), index=True)  # taste_profile|listening_report|insights
    period: Mapped[str] = mapped_column(String(24), default="all")  # week|month|year|all
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    __table_args__ = (
        Index("ix_report_lookup", "user_id", "kind", "period", "created_at"),
    )


class SimilarArtist(Base):
    """Similarity graph pulled from Last.fm / ListenBrainz, cached locally."""

    __tablename__ = "similar_artists"

    id: Mapped[int] = mapped_column(primary_key=True)
    artist_id: Mapped[int] = mapped_column(
        ForeignKey("artists.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(500))
    mbid: Mapped[str | None] = mapped_column(String(36), default=None)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(32), default="lastfm")
    in_library: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("artist_id", "name", "source", name="uq_similar_artist"),
    )


# ─── Misc server state ─────────────────────────────────────────────────────


class InternetRadioStation(Base):
    __tablename__ = "internet_radio_stations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    stream_url: Mapped[str] = mapped_column(Text)
    home_page_url: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Share(Base):
    __tablename__ = "shares"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    description: Mapped[str] = mapped_column(Text, default="")
    item_type: Mapped[str] = mapped_column(String(16), default="track")
    item_ids: Mapped[list] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    visit_count: Mapped[int] = mapped_column(Integer, default=0)
    last_visited_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    scanning: Mapped[bool] = mapped_column(Boolean, default=True)
    full_scan: Mapped[bool] = mapped_column(Boolean, default=False)
    tracks_seen: Mapped[int] = mapped_column(Integer, default=0)
    tracks_added: Mapped[int] = mapped_column(Integer, default=0)
    tracks_updated: Mapped[int] = mapped_column(Integer, default=0)
    tracks_removed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class Setting(Base):
    """Runtime overrides an admin can change without editing .env."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
