import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { usePlayer } from '../store/player'
import type { Album, Artist, Playlist } from '../types'
import { List, Play, Sparkles } from './icons'

export function AlbumCard({ album }: { album: Album }) {
  const { playQueue } = usePlayer()

  async function playAlbum(event: React.MouseEvent) {
    event.preventDefault()
    event.stopPropagation()
    const detail = await api.album(album.id)
    if (detail.tracks.length) playQueue(detail.tracks, 0)
  }

  return (
    <Link
      to={`/albums/${album.id}`}
      className="group block rounded-xl p-3 transition-colors hover:bg-surface"
      data-testid="album-card"
    >
      <div className="relative mb-3 aspect-square overflow-hidden rounded-lg border border-line bg-elevated">
        <img
          src={api.coverUrl('album', album.id, 400)}
          alt=""
          loading="lazy"
          className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-[1.03]"
        />
        <button
          onClick={playAlbum}
          className="absolute bottom-2 right-2 flex h-10 w-10 translate-y-2 items-center justify-center rounded-full bg-accent text-white opacity-0 shadow-lg transition-all group-hover:translate-y-0 group-hover:opacity-100 hover:bg-accent-dim"
          aria-label={`Play ${album.name}`}
          data-testid="play-album"
        >
          <Play className="ml-0.5 h-4 w-4" />
        </button>
      </div>
      <h3 className="truncate text-sm font-medium text-zinc-100" title={album.name}>
        {album.name}
      </h3>
      <p className="truncate text-xs text-muted" title={album.album_artist || album.artist_name}>
        {album.album_artist || album.artist_name}
        {album.year ? ` · ${album.year}` : ''}
      </p>
    </Link>
  )
}

export function ArtistCard({ artist }: { artist: Artist }) {
  return (
    <Link
      to={`/artists/${artist.id}`}
      className="group block rounded-xl p-3 text-center transition-colors hover:bg-surface"
      data-testid="artist-card"
    >
      <div className="mx-auto mb-3 aspect-square overflow-hidden rounded-full border border-line bg-elevated">
        <img
          src={api.coverUrl('artist', artist.id, 400)}
          alt=""
          loading="lazy"
          className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-105"
        />
      </div>
      <h3 className="truncate text-sm font-medium text-zinc-100" title={artist.name}>
        {artist.name}
      </h3>
      <p className="truncate text-xs text-muted">
        {artist.album_count} album{artist.album_count === 1 ? '' : 's'}
      </p>
    </Link>
  )
}

export function PlaylistCard({ playlist }: { playlist: Playlist }) {
  const badge = playlist.is_ai ? 'AI' : playlist.is_smart ? 'Smart' : null

  return (
    <Link
      to={`/playlists/${playlist.id}`}
      className="group block rounded-xl p-3 transition-colors hover:bg-surface"
      data-testid="playlist-card"
    >
      <div className="relative mb-3 flex aspect-square items-center justify-center overflow-hidden rounded-lg border border-line bg-elevated">
        <img
          src={api.coverUrl('playlist', playlist.id, 400)}
          alt=""
          loading="lazy"
          className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-[1.03]"
        />
        {badge && (
          <span className="absolute left-2 top-2 flex items-center gap-1 rounded-full bg-black/70 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-accent-soft backdrop-blur">
            {playlist.is_ai ? <Sparkles className="h-3 w-3" /> : <List className="h-3 w-3" />}
            {badge}
          </span>
        )}
      </div>
      <h3 className="truncate text-sm font-medium text-zinc-100" title={playlist.name}>
        {playlist.name}
      </h3>
      <p className="truncate text-xs text-muted">
        {playlist.song_count} track{playlist.song_count === 1 ? '' : 's'}
      </p>
    </Link>
  )
}

export function Grid({ children }: { children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-2 gap-1 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
      {children}
    </div>
  )
}
