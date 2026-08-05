export interface User {
  id: number
  username: string
  email: string | null
  is_admin: boolean
  is_active: boolean
  max_bitrate: number
  transcode_format: string | null
  lastfm_enabled: boolean
  lastfm_username: string | null
  listenbrainz_enabled: boolean
  ai_enabled: boolean
  created_at: string
  last_login_at: string | null
}

export interface Artist {
  id: number
  name: string
  album_count: number
  track_count: number
  mbid: string | null
  biography: string | null
  image_url: string | null
  has_image: boolean
  starred: boolean
  rating: number
}

export interface Album {
  id: number
  name: string
  artist_id: number | null
  artist_name: string
  album_artist: string
  year: number | null
  genre: string
  song_count: number
  duration: number
  created_at: string
  starred: boolean
  rating: number
  play_count: number
}

export interface Track {
  id: number
  title: string
  album_id: number | null
  album_name: string
  artist_id: number | null
  artist_name: string
  track_number: number
  disc_number: number
  year: number | null
  genre: string
  duration: number
  bitrate: number
  suffix: string
  content_type: string
  size: number
  starred: boolean
  rating: number
  play_count: number
  note?: string | null
}

export interface Playlist {
  id: number
  name: string
  comment: string
  owner_id: number
  owner: string | null
  public: boolean
  is_smart: boolean
  is_ai: boolean
  rules: Record<string, unknown> | null
  ai_prompt: string | null
  ai_rationale: string | null
  song_count: number
  duration: number
  created_at: string
  updated_at: string
  last_generated_at: string | null
}

export interface PlaylistDetail extends Playlist {
  tracks: Track[]
}

export interface Recommendation {
  id: number
  item_type: string
  artist_name: string
  album_name: string
  title: string
  source: string
  score: number
  reason: string
  seed_artist: string
  in_library: boolean
  created_at: string
}

export interface WantedItem {
  id: number
  item_type: string
  artist_name: string
  album_name: string
  title: string
  source: string
  provider: string
  confidence: number
  reason: string
  status: string
  error_message: string | null
  result_path: string | null
  track_id: number | null
  created_at: string
  decided_at: string | null
  completed_at: string | null
}

export interface PodcastChannel {
  id: number
  url: string
  title: string
  description: string
  author: string
  image_url: string | null
  status: string
  error_message: string | null
  auto_download: boolean
  episode_count: number
  last_fetched_at: string | null
}

export interface PodcastEpisode {
  id: number
  channel_id: number
  title: string
  description: string
  publish_date: string | null
  duration: number
  size: number
  status: string
  suffix: string
  error_message: string | null
}

export interface LibraryStats {
  artists: number
  albums: number
  tracks: number
  duration: number
  size: number
  plays: number
}

export interface ServerInfo {
  name: string
  version: string
  subsonic_version: string
  features: Record<string, boolean | string>
}

export interface AnalyticsStats {
  period: string
  totals: {
    plays: number
    listening_seconds: number
    listening_hours: number
    unique_artists: number
    unique_albums: number
    unique_tracks: number
    avg_plays_per_day: number | null
  }
  top_artists: { name: string; plays: number }[]
  top_albums: { name: string; plays: number }[]
  top_tracks: { name: string; plays: number }[]
  top_genres: { name: string; plays: number }[]
  by_hour: Record<string, number>
  by_weekday: Record<string, number>
  daily: { date: string; plays: number }[]
  new_artists: string[]
  new_artist_count: number
  listening_streak_days: number
  repeat_ratio: number
  genre_mix: { genre: string; plays: number; share: number }[]
  comparison: {
    current?: { plays: number; artists: number }
    previous?: { plays: number; artists: number }
    play_change_pct?: number
    artist_change_pct?: number
  }
}

export interface Insights {
  id: number
  period: string
  summary: string
  model: string
  created_at: string
  payload: {
    headline: string
    summary: string
    observations: { title: string; detail: string }[]
    listening_personality: string
    suggestions: string[]
  }
}
