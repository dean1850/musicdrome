/**
 * API client.
 *
 * Holds the access token in memory plus localStorage, and transparently
 * refreshes it once on a 401 before surfacing the failure — so a long-lived tab
 * doesn't bounce the user to the login screen the moment the token ages out.
 */
import type {
  Album,
  AnalyticsStats,
  Artist,
  Insights,
  LibraryStats,
  Playlist,
  PlaylistDetail,
  PodcastChannel,
  PodcastEpisode,
  Recommendation,
  ServerInfo,
  Track,
  User,
  WantedItem,
} from '../types'

const BASE = '/api/v1'

const ACCESS_KEY = 'musicdrome.access'
const REFRESH_KEY = 'musicdrome.refresh'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export const tokens = {
  get access() {
    return localStorage.getItem(ACCESS_KEY) || ''
  },
  get refresh() {
    return localStorage.getItem(REFRESH_KEY) || ''
  },
  set(access: string, refresh: string) {
    localStorage.setItem(ACCESS_KEY, access)
    localStorage.setItem(REFRESH_KEY, refresh)
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY)
    localStorage.removeItem(REFRESH_KEY)
  },
}

let refreshInFlight: Promise<boolean> | null = null

async function refreshAccessToken(): Promise<boolean> {
  if (!tokens.refresh) return false

  // Collapse concurrent 401s into a single refresh round-trip
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const response = await fetch(`${BASE}/auth/refresh`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: tokens.refresh }),
        })
        if (!response.ok) return false
        const data = await response.json()
        tokens.set(data.access_token, data.refresh_token)
        return true
      } catch {
        return false
      } finally {
        // Release the latch on the next tick so followers see the result
        setTimeout(() => {
          refreshInFlight = null
        }, 0)
      }
    })()
  }
  return refreshInFlight
}

async function request<T>(path: string, init: RequestInit = {}, retry = true): Promise<T> {
  const headers = new Headers(init.headers)
  if (tokens.access) headers.set('Authorization', `Bearer ${tokens.access}`)
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(`${BASE}${path}`, { ...init, headers })

  if (response.status === 401 && retry && tokens.refresh) {
    if (await refreshAccessToken()) return request<T>(path, init, false)
    tokens.clear()
    window.dispatchEvent(new CustomEvent('musicdrome:signed-out'))
  }

  if (!response.ok) {
    let detail = `Request failed (${response.status})`
    try {
      const body = await response.json()
      if (typeof body.detail === 'string') detail = body.detail
      else if (Array.isArray(body.detail)) detail = body.detail.map((d: any) => d.msg).join(', ')
    } catch {
      /* non-JSON error body — keep the generic message */
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) return undefined as T
  const contentType = response.headers.get('content-type') || ''
  if (!contentType.includes('application/json')) return (await response.text()) as unknown as T
  return response.json()
}

const get = <T>(path: string) => request<T>(path)
const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })
const put = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'PUT', body: body === undefined ? undefined : JSON.stringify(body) })
const del = <T>(path: string) => request<T>(path, { method: 'DELETE' })

const qs = (params: Record<string, string | number | boolean | undefined>) => {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '' && value !== false) search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export const api = {
  // ── Auth ──
  login: (username: string, password: string) =>
    request<{ access_token: string; refresh_token: string }>(`/auth/login`, {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  register: (username: string, password: string, email?: string) =>
    request<{ access_token: string; refresh_token: string }>(`/auth/register`, {
      method: 'POST',
      body: JSON.stringify({ username, password, email }),
    }),
  me: () => get<User>('/auth/me'),
  updateMe: (payload: Partial<User>) => put<User>('/auth/me', payload),
  changePassword: (current_password: string, new_password: string) =>
    post<{ message: string }>('/auth/password', { current_password, new_password }),
  connectLastFm: (username: string, password: string) =>
    post<{ message: string }>('/auth/lastfm', { username, password }),
  disconnectLastFm: () => del<{ message: string }>('/auth/lastfm'),
  connectListenBrainz: (token: string) => post<{ message: string }>('/auth/listenbrainz', { token }),
  disconnectListenBrainz: () => del<{ message: string }>('/auth/listenbrainz'),

  // ── Library ──
  serverInfo: () => get<ServerInfo>('/server-info'),
  stats: () => get<LibraryStats>('/stats'),
  artists: (params: { q?: string; limit?: number; offset?: number; starred?: boolean } = {}) =>
    get<Artist[]>(`/artists${qs(params)}`),
  artist: (id: number) =>
    get<{ artist: Artist; albums: Album[]; similar: { name: string; score: number; in_library: boolean }[] }>(
      `/artists/${id}`,
    ),
  artistTracks: (id: number) => get<Track[]>(`/artists/${id}/tracks`),
  albums: (params: { q?: string; sort?: string; genre?: string; limit?: number; offset?: number } = {}) =>
    get<Album[]>(`/albums${qs(params)}`),
  album: (id: number) =>
    get<{ album: Album; description: string | null; tracks: Track[] }>(`/albums/${id}`),
  tracks: (params: { q?: string; genre?: string; sort?: string; limit?: number; offset?: number } = {}) =>
    get<Track[]>(`/tracks${qs(params)}`),
  search: (q: string, limit = 20) =>
    get<{ artists: Artist[]; albums: Album[]; tracks: Track[] }>(`/search${qs({ q, limit })}`),
  genres: () => get<{ name: string; track_count: number }[]>('/genres'),
  history: (limit = 50) => get<any[]>(`/history${qs({ limit })}`),

  // ── Playback ──
  streamUrl: (trackId: number) =>
    `${BASE}/stream/${trackId}?token=${encodeURIComponent(tokens.access)}`,
  coverUrl: (kind: 'album' | 'artist' | 'track' | 'playlist' | 'podcast', id: number, size = 0) =>
    `${BASE}/cover/${kind}/${id}${size ? `?size=${size}` : ''}`,
  recordPlay: (trackId: number, submission = true) =>
    post<{ message: string }>(`/play/${trackId}${qs({ submission })}`),
  star: (kind: 'artist' | 'album' | 'track', id: number, starred: boolean) =>
    post<{ message: string }>(`/star/${kind}/${id}${qs({ starred })}`),
  rate: (kind: 'artist' | 'album' | 'track', id: number, rating: number) =>
    post<{ message: string }>(`/rate/${kind}/${id}${qs({ rating })}`),

  // ── Playlists ──
  playlists: (kind: 'all' | 'manual' | 'smart' | 'ai' = 'all') =>
    get<Playlist[]>(`/playlists${qs({ kind })}`),
  playlist: (id: number) => get<PlaylistDetail>(`/playlists/${id}`),
  createPlaylist: (payload: { name: string; comment?: string; public?: boolean; track_ids?: number[] }) =>
    post<Playlist>('/playlists', payload),
  updatePlaylist: (id: number, payload: Record<string, unknown>) =>
    put<Playlist>(`/playlists/${id}`, payload),
  deletePlaylist: (id: number) => del<{ message: string }>(`/playlists/${id}`),
  addToPlaylist: (id: number, trackIds: number[]) => post<Playlist>(`/playlists/${id}/tracks`, trackIds),
  refreshPlaylist: (id: number) => post<Playlist>(`/playlists/${id}/refresh`),
  smartTemplates: () => get<any[]>('/playlists-smart/templates'),
  createSmartPlaylist: (payload: { name: string; comment?: string; public?: boolean; rules: unknown }) =>
    post<Playlist>('/playlists-smart', payload),
  updateSmartPlaylist: (id: number, payload: Record<string, unknown>) =>
    put<Playlist>(`/playlists-smart/${id}`, payload),
  createAIPlaylist: (payload: { brief: string; max_tracks?: number; seed_genre?: string }) =>
    post<Playlist>('/playlists-ai', payload),
  previewAIPlaylist: (payload: { brief: string; max_tracks?: number; seed_genre?: string }) =>
    post<{ name: string; description: string; rationale: string; model: string; tracks: Track[] }>(
      '/playlists-ai/preview',
      payload,
    ),

  // ── Discovery ──
  recommendations: (params: { source?: string; include_owned?: boolean; limit?: number } = {}) =>
    get<Recommendation[]>(`/recommendations${qs(params)}`),
  refreshRecommendations: () => post<{ message: string; data: Record<string, number> }>('/recommendations/refresh'),
  dismissRecommendation: (id: number) => post<{ message: string }>(`/recommendations/${id}/dismiss`),
  wantRecommendation: (id: number) => post<WantedItem>(`/recommendations/${id}/want`),
  wanted: (status?: string) => get<WantedItem[]>(`/wanted${qs({ status })}`),
  createWanted: (payload: { artist: string; title?: string; album?: string; provider?: string; reason?: string }) =>
    post<WantedItem>('/wanted', payload),
  approveWanted: (id: number) => post<WantedItem>(`/wanted/${id}/approve`),
  rejectWanted: (id: number) => post<WantedItem>(`/wanted/${id}/reject`),
  deleteWanted: (id: number) => del<{ message: string }>(`/wanted/${id}`),
  processWanted: () => post<{ message: string }>('/wanted/process'),
  searchDownloadable: (payload: { query: string; artist?: string; title?: string }) =>
    post<{ url: string; title: string; uploader: string; duration: number; score: number }[]>(
      '/acquisition/search',
      payload,
    ),

  // ── Analytics ──
  analytics: (period = 'month') => get<AnalyticsStats>(`/analytics/stats${qs({ period })}`),
  insights: (period = 'month', refresh = false) =>
    get<Insights>(`/analytics/insights${qs({ period, refresh })}`),
  aiStatus: () => get<any>('/ai/status'),
  aiTest: () => post<{ message: string; data: Record<string, string> }>('/ai/test'),

  // ── Podcasts ──
  podcasts: () => get<PodcastChannel[]>('/podcasts'),
  podcast: (id: number) =>
    get<{ channel: PodcastChannel; episodes: PodcastEpisode[] }>(`/podcasts/${id}`),
  subscribePodcast: (url: string) => post<PodcastChannel>('/podcasts', { url }),
  unsubscribePodcast: (id: number) => del<{ message: string }>(`/podcasts/${id}`),
  updatePodcast: (id: number, auto_download: boolean) =>
    put<PodcastChannel>(`/podcasts/${id}`, { auto_download }),
  refreshPodcast: (id: number) => post<PodcastChannel>(`/podcasts/${id}/refresh`),
  refreshAllPodcasts: () => post<{ message: string }>('/podcasts/refresh-all'),
  downloadEpisode: (id: number) => post<{ message: string }>(`/podcasts/episodes/${id}/download`),
  deleteEpisode: (id: number) => del<{ message: string }>(`/podcasts/episodes/${id}`),
  episodeStreamUrl: (id: number) =>
    `${BASE}/podcasts/episodes/${id}/stream?token=${encodeURIComponent(tokens.access)}`,

  // ── Admin ──
  users: () => get<User[]>('/admin/users'),
  createUser: (payload: { username: string; password: string; email?: string; is_admin?: boolean }) =>
    post<User>('/admin/users', payload),
  updateUser: (id: number, payload: Record<string, unknown>) => put<User>(`/admin/users/${id}`, payload),
  deleteUser: (id: number) => del<{ message: string }>(`/admin/users/${id}`),
  scanStatus: () => get<any>('/admin/scan'),
  startScan: (full = false) => post<{ ok: boolean; message: string }>('/admin/scan', { full }),
  jobs: () => get<{ id: string; next_run: string | null; trigger: string }[]>('/admin/jobs'),
  runJob: (id: string) => post<{ message: string }>(`/admin/jobs/${id}/run`),
  integrations: () => get<any>('/admin/integrations'),
  testLidarr: () => post<any>('/admin/integrations/lidarr/test'),
  syncLidarr: () => post<{ message: string; data: any }>('/admin/integrations/lidarr/sync'),
}
