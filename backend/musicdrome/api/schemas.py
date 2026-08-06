"""Request/response models for the native API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ─── Auth ──────────────────────────────────────────────────────────────────


class LoginRequest(BaseModel):
    username: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8)
    email: str | None = None


class PasswordChangeRequest(BaseModel):
    current_password: str | None = None
    new_password: str = Field(min_length=8)


# ─── Users ─────────────────────────────────────────────────────────────────


class UserOut(BaseModel):
    id: int
    username: str
    email: str | None = None
    is_admin: bool
    is_active: bool
    max_bitrate: int
    transcode_format: str | None = None
    lastfm_enabled: bool
    lastfm_username: str | None = None
    listenbrainz_enabled: bool
    ai_enabled: bool
    created_at: datetime
    last_login_at: datetime | None = None

    model_config = {"from_attributes": True}


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8)
    email: str | None = None
    is_admin: bool = False


class UserUpdateRequest(BaseModel):
    email: str | None = None
    is_admin: bool | None = None
    is_active: bool | None = None
    max_bitrate: int | None = None
    transcode_format: str | None = None
    ai_enabled: bool | None = None
    password: str | None = Field(default=None, min_length=8)
    download_role: bool | None = None
    upload_role: bool | None = None
    playlist_role: bool | None = None
    podcast_role: bool | None = None
    share_role: bool | None = None


# ─── Library ───────────────────────────────────────────────────────────────


class ArtistOut(BaseModel):
    id: int
    name: str
    album_count: int
    track_count: int
    mbid: str | None = None
    biography: str | None = None
    image_url: str | None = None
    has_image: bool = False
    starred: bool = False
    rating: int = 0


class AlbumOut(BaseModel):
    id: int
    name: str
    artist_id: int | None = None
    artist_name: str
    album_artist: str
    year: int | None = None
    genre: str
    song_count: int
    duration: int
    created_at: datetime
    starred: bool = False
    rating: int = 0
    play_count: int = 0


class TrackOut(BaseModel):
    id: int
    title: str
    album_id: int | None = None
    album_name: str
    artist_id: int | None = None
    artist_name: str
    track_number: int
    disc_number: int
    year: int | None = None
    genre: str
    duration: int
    bitrate: int
    suffix: str
    content_type: str
    size: int
    starred: bool = False
    rating: int = 0
    play_count: int = 0
    note: str | None = None


class SearchResults(BaseModel):
    artists: list[ArtistOut]
    albums: list[AlbumOut]
    tracks: list[TrackOut]


# ─── Playlists ─────────────────────────────────────────────────────────────


class PlaylistOut(BaseModel):
    id: int
    name: str
    comment: str
    owner_id: int
    owner: str | None = None
    public: bool
    is_smart: bool
    is_ai: bool
    rules: dict | None = None
    ai_prompt: str | None = None
    ai_rationale: str | None = None
    song_count: int
    duration: int
    created_at: datetime
    updated_at: datetime
    last_generated_at: datetime | None = None
    is_imported: bool = False
    import_path: str | None = None
    import_missing: int = 0
    sync: bool = False
    imported_at: datetime | None = None


class PlaylistDetail(PlaylistOut):
    tracks: list[TrackOut] = []


class PlaylistCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    comment: str = ""
    public: bool = False
    track_ids: list[int] = []


class PlaylistUpdateRequest(BaseModel):
    name: str | None = None
    comment: str | None = None
    public: bool | None = None
    track_ids: list[int] | None = None


class SmartPlaylistRequest(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    comment: str = ""
    public: bool = False
    rules: dict


class PlaylistImportRequest(BaseModel):
    # Re-read every playlist file, even ones whose mtime has not moved
    force: bool = False


class AIPlaylistRequest(BaseModel):
    brief: str = Field(min_length=3, max_length=2000)
    max_tracks: int | None = Field(default=None, ge=1, le=500)
    seed_genre: str = ""


# ─── Discovery ─────────────────────────────────────────────────────────────


class RecommendationOut(BaseModel):
    id: int
    item_type: str
    artist_name: str
    album_name: str
    title: str
    source: str
    score: float
    reason: str
    seed_artist: str
    in_library: bool
    created_at: datetime


class WantedItemOut(BaseModel):
    id: int
    item_type: str
    artist_name: str
    album_name: str
    title: str
    source: str
    provider: str
    confidence: float
    reason: str
    status: str
    error_message: str | None = None
    result_path: str | None = None
    track_id: int | None = None
    created_at: datetime
    decided_at: datetime | None = None
    completed_at: datetime | None = None


class WantedCreateRequest(BaseModel):
    artist: str = Field(min_length=1)
    title: str = ""
    album: str = ""
    provider: str = "ytdlp"
    reason: str = ""


class SearchDownloadRequest(BaseModel):
    query: str = Field(min_length=1)
    artist: str = ""
    title: str = ""


# ─── Podcasts ──────────────────────────────────────────────────────────────


class PodcastChannelOut(BaseModel):
    id: int
    url: str
    title: str
    description: str
    author: str
    image_url: str | None = None
    status: str
    error_message: str | None = None
    auto_download: bool
    episode_count: int = 0
    last_fetched_at: datetime | None = None


class PodcastEpisodeOut(BaseModel):
    id: int
    channel_id: int
    title: str
    description: str
    publish_date: datetime | None = None
    duration: int
    size: int
    status: str
    suffix: str
    error_message: str | None = None


class PodcastSubscribeRequest(BaseModel):
    url: str = Field(min_length=4)


# ─── Integrations / settings ───────────────────────────────────────────────


class LastFmLinkRequest(BaseModel):
    username: str
    password: str


class ListenBrainzLinkRequest(BaseModel):
    token: str


class UserSettingsRequest(BaseModel):
    max_bitrate: int | None = Field(default=None, ge=0, le=1411)
    transcode_format: str | None = None
    ai_enabled: bool | None = None
    lastfm_enabled: bool | None = None
    listenbrainz_enabled: bool | None = None


class ScanRequest(BaseModel):
    full: bool = False


class GenericResponse(BaseModel):
    ok: bool = True
    message: str = ""
    data: dict[str, Any] | None = None
