"""Runtime configuration.

Every setting is sourced from the environment (and therefore from ``.env``).
Container deployments set the ``MUSICDROME_*_DIR`` variables to the in-container
paths; a bare-metal or test run falls back to the host-side ``*_DIR`` names, so
both spellings work without duplicating the file.
"""

from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

# Repo root when running from source: backend/musicdrome/config.py -> ../../
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _csv(value: str | list[str] | None) -> list[str]:
    """Parse a comma-separated env value into a clean list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ─── Core server ──────────────────────────────────────────────────────
    port: int = Field(4533, validation_alias="MUSICDROME_PORT")
    host: str = Field("0.0.0.0", validation_alias="MUSICDROME_HOST")
    log_level: str = Field("info", validation_alias="MUSICDROME_LOG_LEVEL")
    base_url: str = Field("", validation_alias="MUSICDROME_BASE_URL")
    server_name: str = Field("Musicdrome", validation_alias="MUSICDROME_SERVER_NAME")
    cors_origins: str = Field("*", validation_alias="MUSICDROME_CORS_ORIGINS")
    workers: int = Field(1, validation_alias="MUSICDROME_WORKERS")
    timezone: str = Field("UTC", validation_alias="TZ")

    # ─── Paths ────────────────────────────────────────────────────────────
    music_dir: Path = Field(
        _REPO_ROOT / "data" / "music",
        validation_alias=AliasChoices("MUSICDROME_MUSIC_DIR", "MUSIC_DIR"),
    )
    data_dir: Path = Field(
        _REPO_ROOT / "data" / "config",
        validation_alias=AliasChoices("MUSICDROME_DATA_DIR", "DATA_DIR"),
    )
    cache_dir: Path = Field(
        _REPO_ROOT / "data" / "cache",
        validation_alias=AliasChoices("MUSICDROME_CACHE_DIR", "CACHE_DIR"),
    )
    podcast_dir: Path = Field(
        _REPO_ROOT / "data" / "podcasts",
        validation_alias=AliasChoices("MUSICDROME_PODCAST_DIR", "PODCAST_DIR"),
    )
    download_dir: Path = Field(
        _REPO_ROOT / "data" / "downloads",
        validation_alias=AliasChoices("MUSICDROME_DOWNLOAD_DIR", "DOWNLOAD_DIR"),
    )
    static_dir: Path = Field(
        _REPO_ROOT / "frontend" / "dist",
        validation_alias="MUSICDROME_STATIC_DIR",
    )
    music_read_only: bool = Field(False, validation_alias="MUSIC_READ_ONLY")

    # ─── Security ─────────────────────────────────────────────────────────
    secret_key: str = Field("", validation_alias="SECRET_KEY")
    credential_encryption_key: str = Field("", validation_alias="CREDENTIAL_ENCRYPTION_KEY")
    jwt_algorithm: str = Field("HS256", validation_alias="JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(1440, validation_alias="ACCESS_TOKEN_EXPIRE_MINUTES")
    refresh_token_expire_days: int = Field(30, validation_alias="REFRESH_TOKEN_EXPIRE_DAYS")
    allow_open_registration: bool = Field(False, validation_alias="ALLOW_OPEN_REGISTRATION")
    default_admin_username: str = Field("admin", validation_alias="DEFAULT_ADMIN_USERNAME")
    default_admin_password: str = Field("", validation_alias="DEFAULT_ADMIN_PASSWORD")
    subsonic_require_token_auth: bool = Field(
        False, validation_alias="SUBSONIC_REQUIRE_TOKEN_AUTH"
    )

    # ─── Scanner ──────────────────────────────────────────────────────────
    scan_on_startup: bool = Field(True, validation_alias="SCAN_ON_STARTUP")
    scan_interval_minutes: int = Field(60, validation_alias="SCAN_INTERVAL_MINUTES")
    scan_watch_filesystem: bool = Field(True, validation_alias="SCAN_WATCH_FILESYSTEM")
    scan_extensions: str = Field(
        "mp3,flac,ogg,oga,opus,m4a,m4b,aac,wav,wma,aiff,aif,ape,mpc,wv",
        validation_alias="SCAN_EXTENSIONS",
    )
    scan_ignore_patterns: str = Field(
        "@eaDir,.AppleDouble,#recycle,.stfolder,lost+found",
        validation_alias="SCAN_IGNORE_PATTERNS",
    )
    cover_art_names: str = Field(
        "cover,folder,front,album,albumart,thumb", validation_alias="COVER_ART_NAMES"
    )
    multivalue_separators: str = Field(
        ";,/,feat.,ft.", validation_alias="MULTIVALUE_SEPARATORS"
    )
    album_grouping: str = Field("musicbrainz", validation_alias="ALBUM_GROUPING")

    # ─── Transcoding ──────────────────────────────────────────────────────
    transcoding_enabled: bool = Field(True, validation_alias="TRANSCODING_ENABLED")
    ffmpeg_path: str = Field("/usr/bin/ffmpeg", validation_alias="FFMPEG_PATH")
    ffprobe_path: str = Field("/usr/bin/ffprobe", validation_alias="FFPROBE_PATH")
    default_transcode_format: str = Field("mp3", validation_alias="DEFAULT_TRANSCODE_FORMAT")
    default_max_bitrate: int = Field(320, validation_alias="DEFAULT_MAX_BITRATE")
    transcode_cache_enabled: bool = Field(True, validation_alias="TRANSCODE_CACHE_ENABLED")
    transcode_cache_size_mb: int = Field(2048, validation_alias="TRANSCODE_CACHE_SIZE_MB")
    transcode_max_concurrent: int = Field(4, validation_alias="TRANSCODE_MAX_CONCURRENT")

    # ─── Last.fm ──────────────────────────────────────────────────────────
    lastfm_enabled: bool = Field(True, validation_alias="LASTFM_ENABLED")
    lastfm_api_key: str = Field("", validation_alias="LASTFM_API_KEY")
    lastfm_api_secret: str = Field("", validation_alias="LASTFM_API_SECRET")
    lastfm_scrobble_enabled: bool = Field(True, validation_alias="LASTFM_SCROBBLE_ENABLED")
    lastfm_fetch_similar: bool = Field(True, validation_alias="LASTFM_FETCH_SIMILAR")
    lastfm_language: str = Field("en", validation_alias="LASTFM_LANGUAGE")

    # ─── ListenBrainz ─────────────────────────────────────────────────────
    listenbrainz_enabled: bool = Field(True, validation_alias="LISTENBRAINZ_ENABLED")
    listenbrainz_api_url: str = Field(
        "https://api.listenbrainz.org", validation_alias="LISTENBRAINZ_API_URL"
    )
    listenbrainz_scrobble_enabled: bool = Field(
        True, validation_alias="LISTENBRAINZ_SCROBBLE_ENABLED"
    )
    listenbrainz_token: str = Field("", validation_alias="LISTENBRAINZ_TOKEN")

    # ─── MusicBrainz ──────────────────────────────────────────────────────
    musicbrainz_enabled: bool = Field(True, validation_alias="MUSICBRAINZ_ENABLED")
    musicbrainz_api_url: str = Field(
        "https://musicbrainz.org/ws/2", validation_alias="MUSICBRAINZ_API_URL"
    )
    musicbrainz_rate_limit: float = Field(1.0, validation_alias="MUSICBRAINZ_RATE_LIMIT")
    musicbrainz_user_agent: str = Field(
        "Musicdrome/1.0.0 ( https://github.com/dean1850/musicdrome )",
        validation_alias="MUSICBRAINZ_USER_AGENT",
    )
    musicbrainz_enrich_mode: str = Field("all", validation_alias="MUSICBRAINZ_ENRICH_MODE")

    # ─── AI ───────────────────────────────────────────────────────────────
    ai_enabled: bool = Field(True, validation_alias="AI_ENABLED")
    ai_provider: Literal["anthropic", "ollama", "openai"] = Field(
        "anthropic", validation_alias="AI_PROVIDER"
    )
    anthropic_api_key: str = Field("", validation_alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field("claude-opus-5", validation_alias="ANTHROPIC_MODEL")
    anthropic_base_url: str = Field(
        "https://api.anthropic.com", validation_alias="ANTHROPIC_BASE_URL"
    )
    anthropic_effort: str = Field("medium", validation_alias="ANTHROPIC_EFFORT")
    ollama_base_url: str = Field("http://ollama:11434", validation_alias="OLLAMA_BASE_URL")
    ollama_model: str = Field("llama3.1", validation_alias="OLLAMA_MODEL")
    openai_base_url: str = Field(
        "https://api.openai.com/v1", validation_alias="OPENAI_BASE_URL"
    )
    openai_api_key: str = Field("", validation_alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4o-mini", validation_alias="OPENAI_MODEL")
    ai_max_tokens: int = Field(8192, validation_alias="AI_MAX_TOKENS")
    ai_temperature: float = Field(0.7, validation_alias="AI_TEMPERATURE")
    ai_request_timeout: int = Field(180, validation_alias="AI_REQUEST_TIMEOUT")
    ai_playlist_refresh_hours: int = Field(24, validation_alias="AI_PLAYLIST_REFRESH_HOURS")
    ai_analytics_refresh_hours: int = Field(24, validation_alias="AI_ANALYTICS_REFRESH_HOURS")
    ai_min_plays_for_profile: int = Field(20, validation_alias="AI_MIN_PLAYS_FOR_PROFILE")
    ai_context_track_limit: int = Field(300, validation_alias="AI_CONTEXT_TRACK_LIMIT")

    # ─── Smart playlists ──────────────────────────────────────────────────
    smart_playlist_enabled: bool = Field(True, validation_alias="SMART_PLAYLIST_ENABLED")
    smart_playlist_refresh_minutes: int = Field(
        60, validation_alias="SMART_PLAYLIST_REFRESH_MINUTES"
    )
    smart_playlist_max_tracks: int = Field(100, validation_alias="SMART_PLAYLIST_MAX_TRACKS")
    smart_playlist_seed_defaults: bool = Field(
        True, validation_alias="SMART_PLAYLIST_SEED_DEFAULTS"
    )

    # ─── Podcasts ─────────────────────────────────────────────────────────
    podcast_enabled: bool = Field(True, validation_alias="PODCAST_ENABLED")
    podcast_refresh_hours: int = Field(6, validation_alias="PODCAST_REFRESH_HOURS")
    podcast_auto_download: bool = Field(False, validation_alias="PODCAST_AUTO_DOWNLOAD")
    podcast_keep_episodes: int = Field(10, validation_alias="PODCAST_KEEP_EPISODES")
    podcast_max_concurrent_downloads: int = Field(
        2, validation_alias="PODCAST_MAX_CONCURRENT_DOWNLOADS"
    )

    # ─── Lidarr ───────────────────────────────────────────────────────────
    lidarr_enabled: bool = Field(False, validation_alias="LIDARR_ENABLED")
    lidarr_url: str = Field("http://lidarr:8686", validation_alias="LIDARR_URL")
    lidarr_api_key: str = Field("", validation_alias="LIDARR_API_KEY")
    lidarr_root_folder: str = Field("/music", validation_alias="LIDARR_ROOT_FOLDER")
    lidarr_quality_profile_id: int = Field(1, validation_alias="LIDARR_QUALITY_PROFILE_ID")
    lidarr_metadata_profile_id: int = Field(1, validation_alias="LIDARR_METADATA_PROFILE_ID")
    lidarr_monitor_mode: str = Field("all", validation_alias="LIDARR_MONITOR_MODE")
    lidarr_sync_interval_minutes: int = Field(30, validation_alias="LIDARR_SYNC_INTERVAL_MINUTES")
    lidarr_push_wanted: bool = Field(True, validation_alias="LIDARR_PUSH_WANTED")
    lidarr_pull_imported: bool = Field(True, validation_alias="LIDARR_PULL_IMPORTED")
    lidarr_search_on_add: bool = Field(True, validation_alias="LIDARR_SEARCH_ON_ADD")

    # ─── Acquisition ──────────────────────────────────────────────────────
    acquisition_enabled: bool = Field(True, validation_alias="ACQUISITION_ENABLED")
    auto_download: bool = Field(False, validation_alias="AUTO_DOWNLOAD")
    ytdlp_format: str = Field("bestaudio/best", validation_alias="YTDLP_FORMAT")
    ytdlp_audio_format: str = Field("mp3", validation_alias="YTDLP_AUDIO_FORMAT")
    ytdlp_audio_quality: str = Field("0", validation_alias="YTDLP_AUDIO_QUALITY")
    ytdlp_cookies_file: str = Field("", validation_alias="YTDLP_COOKIES_FILE")
    ytdlp_proxy: str = Field("", validation_alias="YTDLP_PROXY")
    ytdlp_rate_limit: str = Field("", validation_alias="YTDLP_RATE_LIMIT")
    acquisition_max_concurrent: int = Field(2, validation_alias="ACQUISITION_MAX_CONCURRENT")
    acquisition_min_confidence: float = Field(0.7, validation_alias="ACQUISITION_MIN_CONFIDENCE")
    acquisition_max_per_day: int = Field(25, validation_alias="ACQUISITION_MAX_PER_DAY")
    acquisition_search_prefix: str = Field(
        "ytsearch5", validation_alias="ACQUISITION_SEARCH_PREFIX"
    )
    acquisition_import_template: str = Field(
        "{artist}/{album}/{track:02d} - {title}.{ext}",
        validation_alias="ACQUISITION_IMPORT_TEMPLATE",
    )

    # ─── Recommendations ──────────────────────────────────────────────────
    recommendations_enabled: bool = Field(True, validation_alias="RECOMMENDATIONS_ENABLED")
    recommendation_refresh_hours: int = Field(12, validation_alias="RECOMMENDATION_REFRESH_HOURS")
    recommendation_sources: str = Field(
        "lastfm,listenbrainz,ai", validation_alias="RECOMMENDATION_SOURCES"
    )
    recommendation_limit: int = Field(50, validation_alias="RECOMMENDATION_LIMIT")

    # ─── Testing ──────────────────────────────────────────────────────────
    testing: bool = Field(False, validation_alias="MUSICDROME_TESTING")

    # ─── Validators ───────────────────────────────────────────────────────
    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, v: str) -> str:
        return v.lower()

    @field_validator("base_url")
    @classmethod
    def _normalise_base_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if v and not v.startswith("/"):
            v = "/" + v
        return v

    # ─── Derived helpers ──────────────────────────────────────────────────
    @property
    def extensions(self) -> set[str]:
        """Audio file suffixes the scanner accepts, lowercase, no dot."""
        return {e.lower().lstrip(".") for e in _csv(self.scan_extensions)}

    @property
    def ignore_patterns(self) -> list[str]:
        return _csv(self.scan_ignore_patterns)

    @property
    def cover_names(self) -> list[str]:
        return [n.lower() for n in _csv(self.cover_art_names)]

    @property
    def cors_origin_list(self) -> list[str]:
        return _csv(self.cors_origins) or ["*"]

    @property
    def recommendation_source_list(self) -> list[str]:
        return [s.lower() for s in _csv(self.recommendation_sources)]

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'musicdrome.db'}"

    @property
    def covers_dir(self) -> Path:
        return self.data_dir / "covers"

    @property
    def artists_image_dir(self) -> Path:
        return self.data_dir / "artists"

    @property
    def transcode_cache_dir(self) -> Path:
        return self.cache_dir / "transcodes"

    def ensure_directories(self) -> None:
        """Create everything we own. ``music_dir`` may be read-only, so it is
        only created when it is missing entirely."""
        for path in (
            self.data_dir,
            self.cache_dir,
            self.podcast_dir,
            self.download_dir,
            self.covers_dir,
            self.artists_image_dir,
            self.transcode_cache_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        if not self.music_dir.exists():
            try:
                self.music_dir.mkdir(parents=True, exist_ok=True)
            except OSError:  # read-only mount that has not been provisioned yet
                log.warning("music directory %s does not exist and cannot be created",
                            self.music_dir)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()

    # A missing SECRET_KEY must not stop a first-run container from booting, but
    # an ephemeral key means tokens die on restart — say so loudly.
    if not settings.secret_key:
        settings.secret_key = secrets.token_urlsafe(48)
        log.warning(
            "SECRET_KEY is unset — generated an ephemeral key. "
            "Sessions will not survive a restart. Set SECRET_KEY in .env."
        )

    settings.ensure_directories()
    return settings


settings = get_settings()
