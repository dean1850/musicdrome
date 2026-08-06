import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import TrackList from '../components/TrackList'
import {
  Download,
  FileMusic,
  List,
  Pencil,
  Play,
  Refresh,
  Shuffle,
  Sparkles,
  Trash,
} from '../components/icons'
import { ErrorBanner, Loading, Modal, Spinner, Toast } from '../components/ui'
import { api } from '../lib/api'
import { count, duration as fmtDuration, relativeTime } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { usePlayer } from '../store/player'
import type { Track } from '../types'

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
  const [editOpen, setEditOpen] = useState(false)

  if (loading) return <Loading label="Loading playlist" />
  if (error) return <ErrorBanner message={error} />
  if (!data) return null

  const generated = data.is_smart || data.is_ai
  // A synced playlist can still be edited — doing so just hands it over from
  // the file to the user, which is what the confirmation below explains.
  const editable = !generated

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

  /** Persist a new ordering. The list is short enough to send whole. */
  async function saveTracks(tracks: Track[], message: string) {
    setActionError('')
    try {
      await api.updatePlaylist(playlistId, { track_ids: tracks.map((track) => track.id) })
      await reload()
      setToast(message)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Could not save the playlist')
      await reload()
    }
  }

  function removeTrack(track: Track, index: number) {
    if (!data) return
    const next = data.tracks.filter((_, i) => i !== index)
    void saveTracks(next, `Removed “${track.title}”`)
  }

  function reorder(from: number, to: number) {
    if (!data) return
    const next = [...data.tracks]
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    void saveTracks(next, 'Order saved')
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
            ) : data.is_imported ? (
              <>
                <FileMusic className="h-3.5 w-3.5 text-accent-soft" />
                {data.sync ? 'Imported playlist · synced' : 'Imported playlist'}
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
            {data.is_imported && data.imported_at ? ` · imported ${relativeTime(data.imported_at)}` : ''}
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
            {editable && (
              <button className="btn-outline" onClick={() => setEditOpen(true)} data-testid="edit-playlist">
                <Pencil className="h-4 w-4" /> Edit
              </button>
            )}
            {generated && (
              <button className="btn-outline" onClick={refresh} disabled={busy} data-testid="refresh-playlist">
                {busy ? <Spinner className="h-4 w-4" /> : <Refresh className="h-4 w-4" />} Refresh
              </button>
            )}
            <a
              className="btn-outline"
              href={api.exportPlaylistUrl(data.id)}
              download={`${data.name}.m3u`}
              data-testid="export-playlist"
            >
              <Download className="h-4 w-4" /> Export M3U
            </a>
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

      {data.is_imported && (
        <section className="card p-4" data-testid="import-info">
          <h2 className="mb-1.5 flex items-center gap-2 text-sm font-semibold text-white">
            <FileMusic className="h-4 w-4 text-accent-soft" /> Playlist file
          </h2>
          {data.import_path ? (
            <p className="break-all text-sm text-muted">
              <code className="text-zinc-200">{data.import_path}</code>
            </p>
          ) : (
            <p className="text-sm text-muted">Uploaded by hand — not tied to a file on disk.</p>
          )}
          <p className="mt-2 text-xs text-subtle">
            {data.sync
              ? 'Kept in step with the file: editing the file updates this playlist, and deleting it removes this playlist. Editing the tracks here takes it over instead.'
              : 'No longer following the file — this playlist is yours to edit.'}
          </p>
          {data.import_missing > 0 && (
            <p className="mt-2 text-xs text-amber-300" data-testid="import-missing">
              {count(data.import_missing)}{' '}
              {data.import_missing === 1 ? 'entry is' : 'entries are'} not in your library yet and
              had to be skipped. They will be added on their own once the files are there.
            </p>
          )}
        </section>
      )}

      <TrackList
        tracks={data.tracks}
        showNumber
        showNotes={data.is_ai}
        onRemove={editable ? removeTrack : undefined}
        onReorder={editable ? reorder : undefined}
        emptyLabel={
          generated
            ? 'Nothing matches yet. Refresh after listening more, or loosen the rules.'
            : 'Add tracks from any album or artist page.'
        }
      />

      <EditPlaylistModal
        open={editOpen}
        playlist={data}
        onClose={() => setEditOpen(false)}
        onDone={() => {
          setToast('Playlist updated')
          void reload()
        }}
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
        {data.sync && (
          <p className="mt-3 text-sm text-amber-300">
            Its <code>.m3u</code> file stays on disk, so the next import will bring this playlist
            back. Delete the file too if you want it gone for good.
          </p>
        )}
      </Modal>

      <Toast message={toast} onDone={() => setToast('')} />
    </div>
  )
}

function EditPlaylistModal({
  open,
  playlist,
  onClose,
  onDone,
}: {
  open: boolean
  playlist: { id: number; name: string; comment: string; public: boolean; sync: boolean }
  onClose: () => void
  onDone: () => void
}) {
  const [name, setName] = useState(playlist.name)
  const [comment, setComment] = useState(playlist.comment)
  const [isPublic, setIsPublic] = useState(playlist.public)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit() {
    if (!name.trim()) return
    setBusy(true)
    setError('')
    try {
      await api.updatePlaylist(playlist.id, {
        name: name.trim(),
        comment,
        public: isPublic,
      })
      onDone()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save the playlist')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title="Edit playlist"
      onClose={onClose}
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn-primary" onClick={submit} disabled={busy || !name.trim()} data-testid="save-playlist">
            {busy && <Spinner className="h-4 w-4" />} Save
          </button>
        </>
      }
    >
      <ErrorBanner message={error} />
      <div className="space-y-4">
        <div>
          <label className="label" htmlFor="edit-name">
            Name
          </label>
          <input
            id="edit-name"
            className="input"
            value={name}
            onChange={(event) => setName(event.target.value)}
            autoFocus
            data-testid="edit-playlist-name"
          />
        </div>
        <div>
          <label className="label" htmlFor="edit-comment">
            Description
          </label>
          <input
            id="edit-comment"
            className="input"
            value={comment}
            onChange={(event) => setComment(event.target.value)}
          />
        </div>
        <label className="flex items-center gap-2 text-sm text-muted">
          <input
            type="checkbox"
            checked={isPublic}
            onChange={(event) => setIsPublic(event.target.checked)}
            className="h-4 w-4 rounded border-line bg-elevated"
          />
          Visible to other users on this server
        </label>
        {playlist.sync && (
          <p className="text-xs text-subtle">
            Renaming is safe — the import only sets the name when it first sees the file. Changing
            the track list is what hands the playlist over from the file to you.
          </p>
        )}
      </div>
    </Modal>
  )
}
