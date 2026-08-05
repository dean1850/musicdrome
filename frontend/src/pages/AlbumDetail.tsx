import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import TrackList from '../components/TrackList'
import { Heart, Play, Plus, Shuffle } from '../components/icons'
import { ErrorBanner, Loading, Stars } from '../components/ui'
import { api } from '../lib/api'
import { count, duration as fmtDuration } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { usePlayer } from '../store/player'

export default function AlbumDetail() {
  const { id } = useParams()
  const albumId = Number(id)
  const { playQueue, addToQueue, toggleShuffle, shuffle } = usePlayer()

  const { data, loading, error } = useAsync(() => api.album(albumId), [albumId])
  const [starred, setStarred] = useState<boolean | null>(null)
  const [rating, setRating] = useState<number | null>(null)

  if (loading) return <Loading label="Loading album" />
  if (error) return <ErrorBanner message={error} />
  if (!data) return null

  const { album, description, tracks } = data
  const isStarred = starred ?? album.starred
  const currentRating = rating ?? album.rating

  async function toggleStar() {
    const value = !isStarred
    setStarred(value)
    try {
      await api.star('album', albumId, value)
    } catch {
      setStarred(!value)
    }
  }

  async function rate(value: number) {
    const previous = currentRating
    setRating(value)
    try {
      await api.rate('album', albumId, value)
    } catch {
      setRating(previous)
    }
  }

  function playShuffled() {
    if (!tracks.length) return
    if (!shuffle) toggleShuffle()
    playQueue(tracks, Math.floor(Math.random() * tracks.length))
  }

  return (
    <div className="space-y-8" data-testid="album-detail">
      <header className="flex flex-col gap-6 md:flex-row md:items-end">
        <img
          src={api.coverUrl('album', album.id, 600)}
          alt=""
          className="h-56 w-56 shrink-0 rounded-xl border border-line object-cover shadow-2xl"
        />

        <div className="min-w-0 flex-1">
          <p className="text-xs uppercase tracking-wide text-subtle">Album</p>
          <h1 className="mt-1 text-3xl font-bold tracking-tight text-white" data-testid="album-title">
            {album.name}
          </h1>

          <p className="mt-2 text-sm text-muted">
            {album.artist_id ? (
              <Link to={`/artists/${album.artist_id}`} className="font-medium text-zinc-200 hover:underline">
                {album.album_artist || album.artist_name}
              </Link>
            ) : (
              <span className="font-medium text-zinc-200">{album.album_artist || album.artist_name}</span>
            )}
            {album.year ? ` · ${album.year}` : ''}
            {album.genre ? ` · ${album.genre}` : ''}
            {` · ${count(album.song_count)} tracks · ${fmtDuration(album.duration)}`}
          </p>

          {description && (
            <p className="mt-3 max-w-2xl text-sm leading-relaxed text-muted line-clamp-4">{description}</p>
          )}

          <div className="mt-5 flex flex-wrap items-center gap-2">
            <button
              className="btn-primary"
              onClick={() => playQueue(tracks, 0)}
              disabled={!tracks.length}
              data-testid="play-album"
            >
              <Play className="h-4 w-4" /> Play
            </button>
            <button className="btn-outline" onClick={playShuffled} disabled={!tracks.length}>
              <Shuffle className="h-4 w-4" /> Shuffle
            </button>
            <button className="btn-outline" onClick={() => addToQueue(tracks)} disabled={!tracks.length}>
              <Plus className="h-4 w-4" /> Queue
            </button>
            <button
              className={`btn-outline ${isStarred ? 'text-accent-soft' : ''}`}
              onClick={toggleStar}
              data-testid="star-album"
            >
              <Heart className="h-4 w-4" filled={isStarred} />
              {isStarred ? 'Favourited' : 'Favourite'}
            </button>
            <span className="ml-1">
              <Stars value={currentRating} onChange={rate} />
            </span>
          </div>
        </div>
      </header>

      <TrackList tracks={tracks} hideAlbum hideArtist={tracks.every((t) => t.artist_name === album.artist_name)} showNumber />
    </div>
  )
}
