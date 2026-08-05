import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import TrackList from '../components/TrackList'
import { List, Play, Refresh, Shuffle, Sparkles, Trash } from '../components/icons'
import { ErrorBanner, Loading, Modal, Spinner, Toast } from '../components/ui'
import { api } from '../lib/api'
import { count, duration as fmtDuration, relativeTime } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { usePlayer } from '../store/player'

export default function PlaylistDetail() {
  const { id } = useParams()
  const playlistId = Number(id)
  const navigate = useNavigate()
  const { playQueue, addToQueue, toggleShuffle, shuffle } = usePlayer()

  const { data, loading, error, reload } = useAsync(() => api.playlist(playlistId), [playlistId])
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState('')
  const [actionError, setActionError] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)

  if (loading) return <Loading label="Loading playlist" />
  if (error) return <ErrorBanner message={error} />
  if (!data) return null

  const generated = data.is_smart || data.is_ai

  async function refresh() {
    setBusy(true)
    setActionError('')
    try {
      await api.refreshPlaylist(playlistId)
      await reload()
      setToast('Playlist refreshed')
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Refresh failed')
    } finally {
      setBusy(false)
    }
  }

  async function remove() {
    setBusy(true)
    try {
      await api.deletePlaylist(playlistId)
      navigate('/playlists')
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Could not delete the playlist')
      setBusy(false)
    }
  }

  return (
    <div className="space-y-8" data-testid="playlist-detail">
      <header className="flex flex-col gap-6 md:flex-row md:items-end">
        <img
          src={api.coverUrl('playlist', data.id, 500)}
          alt=""
          className="h-48 w-48 shrink-0 rounded-xl border border-line object-cover shadow-2xl"
        />

        <div className="min-w-0 flex-1">
          <p className="flex items-center gap-2 text-xs uppercase tracking-wide text-subtle">
            {data.is_ai ? (
              <>
                <Sparkles className="h-3.5 w-3.5 text-accent-soft" /> AI playlist
              </>
            ) : data.is_smart ? (
              <>
                <List className="h-3.5 w-3.5 text-accent-soft" /> Smart playlist
              </>
            ) : (
              'Playlist'
            )}
          </p>
          <h1 className="mt-1 text-3xl font-bold tracking-tight text-white" data-testid="playlist-title">
            {data.name}
          </h1>
          {data.comment && <p className="mt-2 max-w-2xl text-sm text-muted">{data.comment}</p>}
          <p className="mt-2 text-sm text-subtle">
            {data.owner ? `${data.owner} · ` : ''}
            {count(data.song_count)} tracks · {fmtDuration(data.duration)}
            {generated && data.last_generated_at ? ` · refreshed ${relativeTime(data.last_generated_at)}` : ''}
          </p>

          <div className="mt-5 flex flex-wrap items-center gap-2">
            <button
              className="btn-primary"
              onClick={() => playQueue(data.tracks, 0)}
              disabled={!data.tracks.length}
              data-testid="play-playlist"
            >
              <Play className="h-4 w-4" /> Play
            </button>
            <button
              className="btn-outline"
              disabled={!data.tracks.length}
              onClick={() => {
                if (!shuffle) toggleShuffle()
                playQueue(data.tracks, Math.floor(Math.random() * data.tracks.length))
              }}
            >
              <Shuffle className="h-4 w-4" /> Shuffle
            </button>
            <button className="btn-outline" onClick={() => addToQueue(data.tracks)} disabled={!data.tracks.length}>
              Queue
            </button>
            {generated && (
              <button className="btn-outline" onClick={refresh} disabled={busy} data-testid="refresh-playlist">
                {busy ? <Spinner className="h-4 w-4" /> : <Refresh className="h-4 w-4" />} Refresh
              </button>
            )}
            <button
              className="btn-outline text-red-300 hover:border-red-800 hover:text-red-200"
              onClick={() => setConfirmDelete(true)}
              data-testid="delete-playlist"
            >
              <Trash className="h-4 w-4" /> Delete
            </button>
          </div>
        </div>
      </header>

      <ErrorBanner message={actionError} onDismiss={() => setActionError('')} />

      {data.is_ai && data.ai_rationale && (
        <section className="card p-4">
          <h2 className="mb-1.5 flex items-center gap-2 text-sm font-semibold text-white">
            <Sparkles className="h-4 w-4 text-accent-soft" /> Why these tracks
          </h2>
          <p className="text-sm leading-relaxed text-muted">{data.ai_rationale}</p>
          {data.ai_prompt && (
            <p className="mt-2 text-xs text-subtle">
              Brief: <span className="italic">“{data.ai_prompt}”</span>
            </p>
          )}
        </section>
      )}

      {data.is_smart && data.rules && (
        <section className="card p-4">
          <h2 className="mb-1.5 text-sm font-semibold text-white">Rules</h2>
          <pre className="overflow-x-auto rounded-lg bg-elevated p-3 text-xs text-muted">
            {JSON.stringify(data.rules, null, 2)}
          </pre>
        </section>
      )}

      <TrackList
        tracks={data.tracks}
        showNumber
        showNotes={data.is_ai}
        emptyLabel={
          generated
            ? 'Nothing matches yet. Refresh after listening more, or loosen the rules.'
            : 'Add tracks from any album or artist page.'
        }
      />

      <Modal
        open={confirmDelete}
        title="Delete playlist"
        onClose={() => setConfirmDelete(false)}
        footer={
          <>
            <button className="btn-ghost" onClick={() => setConfirmDelete(false)}>
              Cancel
            </button>
            <button className="btn-danger" onClick={remove} disabled={busy} data-testid="confirm-delete">
              {busy && <Spinner className="h-4 w-4" />} Delete
            </button>
          </>
        }
      >
        <p className="text-sm text-muted">
          Delete <span className="font-medium text-zinc-100">{data.name}</span>? The tracks stay in
          your library — only the playlist is removed.
        </p>
      </Modal>

      <Toast message={toast} onDone={() => setToast('')} />
    </div>
  )
}
