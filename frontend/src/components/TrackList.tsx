import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { duration as fmtDuration } from '../lib/format'
import { usePlayer } from '../store/player'
import type { Track } from '../types'
import { Heart, Play, Plus } from './icons'
import { EmptyState, Modal, Stars } from './ui'
import type { Playlist } from '../types'

interface Props {
  tracks: Track[]
  /** Hide columns that would repeat the page's own context. */
  hideArtist?: boolean
  hideAlbum?: boolean
  showNumber?: boolean
  showNotes?: boolean
  emptyLabel?: string
}

export default function TrackList({
  tracks,
  hideArtist = false,
  hideAlbum = false,
  showNumber = false,
  showNotes = false,
  emptyLabel = 'No tracks here yet.',
}: Props) {
  const { playQueue, current, playing } = usePlayer()
  const [starred, setStarred] = useState<Record<number, boolean>>({})
  const [ratings, setRatings] = useState<Record<number, number>>({})
  const [addTarget, setAddTarget] = useState<Track | null>(null)

  const currentTrack = current()

  if (!tracks.length) {
    return <EmptyState title="Nothing to play" description={emptyLabel} />
  }

  const isStarred = (track: Track) => starred[track.id] ?? track.starred
  const ratingOf = (track: Track) => ratings[track.id] ?? track.rating

  async function toggleStar(track: Track) {
    const value = !isStarred(track)
    setStarred((prev) => ({ ...prev, [track.id]: value }))
    try {
      await api.star('track', track.id, value)
    } catch {
      setStarred((prev) => ({ ...prev, [track.id]: !value }))
    }
  }

  async function rate(track: Track, rating: number) {
    const previous = ratingOf(track)
    setRatings((prev) => ({ ...prev, [track.id]: rating }))
    try {
      await api.rate('track', track.id, rating)
    } catch {
      setRatings((prev) => ({ ...prev, [track.id]: previous }))
    }
  }

  // Column template mirrors what's actually rendered
  const columns = [
    showNumber ? '2rem' : null,
    '2rem',
    'minmax(0,3fr)',
    hideArtist ? null : 'minmax(0,2fr)',
    hideAlbum ? null : 'minmax(0,2fr)',
    '7rem',
    '3.5rem',
    '2rem',
    '2rem',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <>
      <div className="w-full" data-testid="track-list">
        <div
          className="grid gap-3 border-b border-line px-3 pb-2 text-[11px] uppercase tracking-wide text-subtle"
          style={{ gridTemplateColumns: columns }}
        >
          {showNumber && <span className="text-right">#</span>}
          <span />
          <span>Title</span>
          {!hideArtist && <span>Artist</span>}
          {!hideAlbum && <span>Album</span>}
          <span>Rating</span>
          <span className="text-right">Time</span>
          <span />
          <span />
        </div>

        <ul className="mt-1">
          {tracks.map((track, i) => {
            const isCurrent = currentTrack?.id === track.id
            return (
              <li key={`${track.id}-${i}`}>
                <div
                  className={`track-row group ${isCurrent ? 'bg-elevated' : ''}`}
                  style={{ gridTemplateColumns: columns }}
                  data-testid="track-row"
                  data-track-id={track.id}
                >
                  {showNumber && (
                    <span className="text-right text-xs tabular-nums text-subtle">
                      {track.track_number || i + 1}
                    </span>
                  )}

                  <button
                    onClick={() => playQueue(tracks, i)}
                    className={`flex h-7 w-7 items-center justify-center rounded-full transition ${
                      isCurrent && playing
                        ? 'text-accent-soft'
                        : 'text-subtle opacity-0 group-hover:opacity-100 hover:text-white'
                    }`}
                    aria-label={`Play ${track.title}`}
                    data-testid="play-track"
                  >
                    {isCurrent && playing ? (
                      <span className="flex h-3.5 items-end gap-[2px]" aria-hidden="true">
                        <i className="block w-[3px] animate-bar rounded-sm bg-current" style={{ height: '100%', animationDelay: '0ms' }} />
                        <i className="block w-[3px] animate-bar rounded-sm bg-current" style={{ height: '70%', animationDelay: '150ms' }} />
                        <i className="block w-[3px] animate-bar rounded-sm bg-current" style={{ height: '100%', animationDelay: '300ms' }} />
                      </span>
                    ) : (
                      <Play className="h-3.5 w-3.5" />
                    )}
                  </button>

                  <div className="min-w-0">
                    <button
                      onClick={() => playQueue(tracks, i)}
                      className={`block w-full truncate text-left ${isCurrent ? 'text-accent-soft' : 'text-zinc-100'}`}
                    >
                      {track.title}
                    </button>
                    {showNotes && track.note && (
                      <p className="truncate text-xs italic text-subtle" title={track.note}>
                        {track.note}
                      </p>
                    )}
                  </div>

                  {!hideArtist && (
                    <span className="min-w-0 truncate text-muted">
                      {track.artist_id ? (
                        <Link to={`/artists/${track.artist_id}`} className="hover:text-zinc-100 hover:underline">
                          {track.artist_name}
                        </Link>
                      ) : (
                        track.artist_name
                      )}
                    </span>
                  )}

                  {!hideAlbum && (
                    <span className="min-w-0 truncate text-muted">
                      {track.album_id ? (
                        <Link to={`/albums/${track.album_id}`} className="hover:text-zinc-100 hover:underline">
                          {track.album_name}
                        </Link>
                      ) : (
                        track.album_name
                      )}
                    </span>
                  )}

                  <span className="opacity-60 transition-opacity group-hover:opacity-100">
                    <Stars value={ratingOf(track)} onChange={(value) => rate(track, value)} size="h-3.5 w-3.5" />
                  </span>

                  <span className="text-right text-xs tabular-nums text-subtle">
                    {fmtDuration(track.duration)}
                  </span>

                  <button
                    onClick={() => toggleStar(track)}
                    className={`${
                      isStarred(track) ? 'text-accent-soft' : 'text-subtle opacity-0 group-hover:opacity-100'
                    } hover:text-accent-soft`}
                    aria-label={isStarred(track) ? 'Remove from favourites' : 'Add to favourites'}
                    data-testid="star-track"
                  >
                    <Heart className="h-4 w-4" filled={isStarred(track)} />
                  </button>

                  <button
                    onClick={() => setAddTarget(track)}
                    className="text-subtle opacity-0 transition-opacity group-hover:opacity-100 hover:text-white"
                    aria-label="Add to playlist"
                    data-testid="add-to-playlist"
                  >
                    <Plus className="h-4 w-4" />
                  </button>
                </div>
              </li>
            )
          })}
        </ul>
      </div>

      <AddToPlaylistModal track={addTarget} onClose={() => setAddTarget(null)} />
    </>
  )
}

function AddToPlaylistModal({ track, onClose }: { track: Track | null; onClose: () => void }) {
  const [playlists, setPlaylists] = useState<Playlist[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState('')

  if (track && playlists === null && !busy) {
    setBusy(true)
    void api
      .playlists('manual')
      .then(setPlaylists)
      .catch(() => setPlaylists([]))
      .finally(() => setBusy(false))
  }

  async function add(playlist: Playlist) {
    if (!track) return
    await api.addToPlaylist(playlist.id, [track.id])
    setDone(`Added to ${playlist.name}`)
    setTimeout(() => {
      setDone('')
      onClose()
    }, 900)
  }

  return (
    <Modal open={!!track} title="Add to playlist" onClose={onClose}>
      {done ? (
        <p className="py-6 text-center text-sm text-accent-soft">{done}</p>
      ) : !playlists ? (
        <p className="py-6 text-center text-sm text-muted">Loading playlists…</p>
      ) : playlists.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted">
          You have no editable playlists yet. Create one from the Playlists page.
        </p>
      ) : (
        <ul className="space-y-1">
          {playlists.map((playlist) => (
            <li key={playlist.id}>
              <button
                onClick={() => add(playlist)}
                className="flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-sm hover:bg-elevated"
              >
                <span className="text-zinc-100">{playlist.name}</span>
                <span className="text-xs text-subtle">{playlist.song_count} tracks</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  )
}
