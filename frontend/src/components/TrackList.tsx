import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { duration as fmtDuration } from '../lib/format'
import { usePlayer } from '../store/player'
import type { Track } from '../types'
import { Grip, Heart, Play, Plus, X } from './icons'
import { EmptyState, ErrorBanner, Modal, Stars } from './ui'
import type { Playlist } from '../types'

interface Props {
  tracks: Track[]
  /** Hide columns that would repeat the page's own context. */
  hideArtist?: boolean
  hideAlbum?: boolean
  showNumber?: boolean
  showNotes?: boolean
  emptyLabel?: string
  /** Supplying either of these turns the list into a playlist editor. */
  onRemove?: (track: Track, index: number) => void
  onReorder?: (from: number, to: number) => void
}

export default function TrackList({
  tracks,
  hideArtist = false,
  hideAlbum = false,
  showNumber = false,
  showNotes = false,
  emptyLabel = 'No tracks here yet.',
  onRemove,
  onReorder,
}: Props) {
  const { playQueue, current, playing } = usePlayer()
  const [starred, setStarred] = useState<Record<number, boolean>>({})
  const [ratings, setRatings] = useState<Record<number, number>>({})
  const [addTarget, setAddTarget] = useState<Track | null>(null)
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [overIndex, setOverIndex] = useState<number | null>(null)

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

  function move(from: number, to: number) {
    if (!onReorder) return
    if (to < 0 || to >= tracks.length || to === from) return
    onReorder(from, to)
  }

  // Column template mirrors what's actually rendered
  const columns = [
    onReorder ? '1.5rem' : null,
    showNumber ? '2rem' : null,
    '2rem',
    'minmax(0,3fr)',
    hideArtist ? null : 'minmax(0,2fr)',
    hideAlbum ? null : 'minmax(0,2fr)',
    '7rem',
    '3.5rem',
    '2rem',
    '2rem',
    onRemove ? '2rem' : null,
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
          {onReorder && <span />}
          {showNumber && <span className="text-right">#</span>}
          <span />
          <span>Title</span>
          {!hideArtist && <span>Artist</span>}
          {!hideAlbum && <span>Album</span>}
          <span>Rating</span>
          <span className="text-right">Time</span>
          <span />
          <span />
          {onRemove && <span />}
        </div>

        <ul className="mt-1">
          {tracks.map((track, i) => {
            const isCurrent = currentTrack?.id === track.id
            const isDropTarget = overIndex === i && dragIndex !== null && dragIndex !== i
            return (
              <li
                key={`${track.id}-${i}`}
                draggable={Boolean(onReorder)}
                onDragStart={(event) => {
                  setDragIndex(i)
                  event.dataTransfer.effectAllowed = 'move'
                }}
                onDragOver={(event) => {
                  if (dragIndex === null) return
                  event.preventDefault()
                  setOverIndex(i)
                }}
                onDrop={(event) => {
                  event.preventDefault()
                  if (dragIndex !== null) move(dragIndex, i)
                  setDragIndex(null)
                  setOverIndex(null)
                }}
                onDragEnd={() => {
                  setDragIndex(null)
                  setOverIndex(null)
                }}
                className={`${isDropTarget ? 'border-t-2 border-accent' : ''} ${
                  dragIndex === i ? 'opacity-40' : ''
                }`}
              >
                <div
                  className={`track-row group ${isCurrent ? 'bg-elevated' : ''}`}
                  style={{ gridTemplateColumns: columns }}
                  data-testid="track-row"
                  data-track-id={track.id}
                >
                  {onReorder && (
                    <button
                      className="cursor-grab text-subtle opacity-0 transition-opacity group-hover:opacity-100 hover:text-white focus:opacity-100"
                      aria-label={`Reorder ${track.title}. Use the arrow keys to move it.`}
                      title="Drag to reorder, or use the arrow keys"
                      data-testid="reorder-track"
                      onKeyDown={(event) => {
                        if (event.key === 'ArrowUp') {
                          event.preventDefault()
                          move(i, i - 1)
                        } else if (event.key === 'ArrowDown') {
                          event.preventDefault()
                          move(i, i + 1)
                        }
                      }}
                    >
                      <Grip className="h-4 w-4" />
                    </button>
                  )}

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

                  {onRemove && (
                    <button
                      onClick={() => onRemove(track, i)}
                      className="text-subtle opacity-0 transition-opacity group-hover:opacity-100 hover:text-red-300"
                      aria-label={`Remove ${track.title} from this playlist`}
                      data-testid="remove-track"
                    >
                      <X className="h-4 w-4" />
                    </button>
                  )}
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
  const [done, setDone] = useState('')
  const [error, setError] = useState('')
  const [newName, setNewName] = useState('')
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    if (!track) return
    let cancelled = false
    setPlaylists(null)
    setError('')
    void api
      .playlists('manual')
      .then((result) => !cancelled && setPlaylists(result))
      .catch(() => !cancelled && setPlaylists([]))
    return () => {
      cancelled = true
    }
  }, [track])

  function confirm(name: string) {
    setDone(`Added to ${name}`)
    setTimeout(() => {
      setDone('')
      setNewName('')
      onClose()
    }, 900)
  }

  async function add(playlist: Playlist) {
    if (!track) return
    try {
      await api.addToPlaylist(playlist.id, [track.id])
      confirm(playlist.name)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not add the track')
    }
  }

  async function createAndAdd() {
    if (!track || !newName.trim()) return
    setCreating(true)
    setError('')
    try {
      const playlist = await api.createPlaylist({
        name: newName.trim(),
        track_ids: [track.id],
      })
      confirm(playlist.name)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create the playlist')
    } finally {
      setCreating(false)
    }
  }

  return (
    <Modal open={!!track} title="Add to playlist" onClose={onClose}>
      {done ? (
        <p className="py-6 text-center text-sm text-accent-soft">{done}</p>
      ) : (
        <div className="space-y-4">
          <ErrorBanner message={error} onDismiss={() => setError('')} />

          <div className="flex gap-2">
            <input
              className="input"
              placeholder="New playlist name…"
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
              onKeyDown={(event) => event.key === 'Enter' && void createAndAdd()}
              data-testid="quick-playlist-name"
            />
            <button
              className="btn-outline shrink-0"
              onClick={createAndAdd}
              disabled={creating || !newName.trim()}
              data-testid="quick-playlist-create"
            >
              <Plus className="h-4 w-4" /> Create
            </button>
          </div>

          {!playlists ? (
            <p className="py-4 text-center text-sm text-muted">Loading playlists…</p>
          ) : playlists.length === 0 ? (
            <p className="py-4 text-center text-sm text-muted">
              No editable playlists yet — name one above to start.
            </p>
          ) : (
            <ul className="space-y-1 border-t border-line pt-3">
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
        </div>
      )}
    </Modal>
  )
}
