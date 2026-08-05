import { useState } from 'react'
import { Check, Compass, Download, Plus, Refresh, Sparkles, Trash, X } from '../components/icons'
import { EmptyState, ErrorBanner, Loading, Modal, Spinner, Tabs, Toast } from '../components/ui'
import { api } from '../lib/api'
import { relativeTime } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { useAuth } from '../store/auth'

const STATUS_STYLES: Record<string, string> = {
  pending: 'text-amber-300 border-amber-900/60 bg-amber-950/30',
  approved: 'text-sky-300 border-sky-900/60 bg-sky-950/30',
  downloading: 'text-sky-300 border-sky-900/60 bg-sky-950/30',
  imported: 'text-emerald-300 border-emerald-900/60 bg-emerald-950/30',
  rejected: 'text-zinc-400 border-line bg-elevated',
  failed: 'text-red-300 border-red-900/60 bg-red-950/30',
}

export default function Discover() {
  const { server } = useAuth()
  const [tab, setTab] = useState<'recommendations' | 'wanted'>('recommendations')
  const [toast, setToast] = useState('')

  return (
    <div className="space-y-5" data-testid="discover-page">
      <header>
        <h1 className="page-title">Discover</h1>
        <p className="mt-1 text-sm text-muted">
          Suggestions drawn from Last.fm, ListenBrainz and the AI, plus the queue of things you've
          asked Musicdrome to go and find.
        </p>
      </header>

      <Tabs
        value={tab}
        onChange={setTab}
        options={[
          { value: 'recommendations', label: 'Recommendations' },
          { value: 'wanted', label: 'Wanted' },
        ]}
      />

      {tab === 'recommendations' ? (
        <Recommendations onToast={setToast} />
      ) : (
        <Wanted onToast={setToast} acquisitionEnabled={Boolean(server?.features?.acquisition)} />
      )}

      <Toast message={toast} onDone={() => setToast('')} />
    </div>
  )
}

function Recommendations({ onToast }: { onToast: (message: string) => void }) {
  const { data, loading, error, reload } = useAsync(() => api.recommendations({ limit: 60 }), [])
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState('')

  async function refresh() {
    setBusy(true)
    setActionError('')
    try {
      const result = await api.refreshRecommendations()
      onToast(result.message)
      await reload()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Could not refresh recommendations')
    } finally {
      setBusy(false)
    }
  }

  async function want(id: number, label: string) {
    try {
      await api.wantRecommendation(id)
      onToast(`Added ${label} to the wanted queue`)
      await reload()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Could not queue that item')
    }
  }

  async function dismiss(id: number) {
    await api.dismissRecommendation(id)
    await reload()
  }

  if (loading) return <Loading label="Loading recommendations" />

  return (
    <div className="space-y-4">
      <ErrorBanner message={error || actionError} onDismiss={() => setActionError('')} />

      <div className="flex justify-end">
        <button className="btn-outline" onClick={refresh} disabled={busy} data-testid="refresh-recommendations">
          {busy ? <Spinner className="h-4 w-4" /> : <Refresh className="h-4 w-4" />} Refresh
        </button>
      </div>

      {!data?.length ? (
        <EmptyState
          icon={<Compass className="h-10 w-10" />}
          title="No recommendations yet"
          description="Recommendations build from your listening history. Play some music, connect Last.fm or ListenBrainz, then refresh."
          action={
            <button className="btn-primary" onClick={refresh} disabled={busy}>
              <Refresh className="h-4 w-4" /> Generate now
            </button>
          }
        />
      ) : (
        <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {data.map((item) => (
            <li key={item.id} className="card flex flex-col gap-2 p-4" data-testid="recommendation">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="truncate font-medium text-zinc-100">
                    {item.title || item.artist_name}
                  </h3>
                  {item.title && <p className="truncate text-sm text-muted">{item.artist_name}</p>}
                  {item.album_name && <p className="truncate text-xs text-subtle">{item.album_name}</p>}
                </div>
                <span className="chip shrink-0 gap-1">
                  {item.source === 'ai' && <Sparkles className="h-3 w-3" />}
                  {item.source}
                </span>
              </div>

              {item.reason && <p className="text-xs leading-relaxed text-muted">{item.reason}</p>}

              <div className="mt-auto flex items-center justify-between pt-2">
                <span className="text-xs text-subtle">
                  {Math.round(item.score * 100)}% match
                  {item.seed_artist ? ` · via ${item.seed_artist}` : ''}
                </span>
                <div className="flex gap-1">
                  <button
                    onClick={() => want(item.id, item.title || item.artist_name)}
                    className="btn-ghost px-2 py-1 text-xs"
                    data-testid="want-recommendation"
                  >
                    <Download className="h-3.5 w-3.5" /> Want
                  </button>
                  <button
                    onClick={() => dismiss(item.id)}
                    className="btn-ghost px-2 py-1 text-xs text-subtle"
                    aria-label="Dismiss"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function Wanted({
  onToast,
  acquisitionEnabled,
}: {
  onToast: (message: string) => void
  acquisitionEnabled: boolean
}) {
  const { data, loading, error, reload } = useAsync(() => api.wanted(), [])
  const [addOpen, setAddOpen] = useState(false)
  const [actionError, setActionError] = useState('')
  const [busy, setBusy] = useState(false)

  async function act(fn: () => Promise<unknown>, message: string) {
    setActionError('')
    try {
      await fn()
      onToast(message)
      await reload()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'That action failed')
    }
  }

  async function processQueue() {
    setBusy(true)
    await act(() => api.processWanted(), 'Download worker started')
    setBusy(false)
  }

  if (loading) return <Loading label="Loading wanted queue" />

  return (
    <div className="space-y-4">
      <ErrorBanner message={error || actionError} onDismiss={() => setActionError('')} />

      <div className="flex flex-wrap justify-between gap-2">
        <p className="text-sm text-muted">
          {acquisitionEnabled
            ? 'Approved items are fetched in the background and imported into your library.'
            : 'Acquisition is disabled — set ACQUISITION_ENABLED=true in .env to fetch these automatically.'}
        </p>
        <div className="flex gap-2">
          <button className="btn-outline" onClick={() => setAddOpen(true)} data-testid="add-wanted">
            <Plus className="h-4 w-4" /> Add
          </button>
          <button
            className="btn-outline"
            onClick={processQueue}
            disabled={busy || !acquisitionEnabled}
            data-testid="process-queue"
          >
            {busy ? <Spinner className="h-4 w-4" /> : <Download className="h-4 w-4" />} Run now
          </button>
        </div>
      </div>

      {!data?.length ? (
        <EmptyState
          icon={<Download className="h-10 w-10" />}
          title="Nothing wanted right now"
          description="Mark a recommendation as wanted, or add an artist or track by hand."
          action={
            <button className="btn-primary" onClick={() => setAddOpen(true)}>
              <Plus className="h-4 w-4" /> Add something
            </button>
          }
        />
      ) : (
        <ul className="space-y-2">
          {data.map((item) => (
            <li
              key={item.id}
              className="card flex flex-wrap items-center gap-3 px-4 py-3"
              data-testid="wanted-item"
            >
              <div className="min-w-0 flex-1">
                <h3 className="truncate font-medium text-zinc-100">
                  {item.title || item.album_name || item.artist_name}
                </h3>
                <p className="truncate text-sm text-muted">
                  {item.title || item.album_name ? item.artist_name : item.item_type}
                  {item.reason ? ` · ${item.reason}` : ''}
                </p>
                {item.error_message && (
                  <p className="mt-0.5 truncate text-xs text-red-300">{item.error_message}</p>
                )}
              </div>

              <span className="chip shrink-0">{item.provider}</span>
              <span
                className={`shrink-0 rounded-full border px-2.5 py-0.5 text-xs ${
                  STATUS_STYLES[item.status] || 'border-line bg-elevated text-muted'
                }`}
                data-testid="wanted-status"
              >
                {item.status}
              </span>
              <span className="hidden shrink-0 text-xs text-subtle sm:block">
                {relativeTime(item.created_at)}
              </span>

              <div className="flex shrink-0 gap-1">
                {item.status === 'pending' && (
                  <>
                    <button
                      className="btn-ghost px-2 py-1 text-xs text-emerald-300"
                      onClick={() => act(() => api.approveWanted(item.id), 'Approved')}
                      data-testid="approve-wanted"
                    >
                      <Check className="h-3.5 w-3.5" /> Approve
                    </button>
                    <button
                      className="btn-ghost px-2 py-1 text-xs text-subtle"
                      onClick={() => act(() => api.rejectWanted(item.id), 'Rejected')}
                    >
                      <X className="h-3.5 w-3.5" /> Reject
                    </button>
                  </>
                )}
                <button
                  className="btn-ghost px-2 py-1 text-xs text-subtle hover:text-red-400"
                  onClick={() => act(() => api.deleteWanted(item.id), 'Removed')}
                  aria-label="Remove"
                >
                  <Trash className="h-3.5 w-3.5" />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <AddWantedModal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        onDone={(label) => {
          onToast(`Queued ${label}`)
          void reload()
        }}
      />
    </div>
  )
}

function AddWantedModal({
  open,
  onClose,
  onDone,
}: {
  open: boolean
  onClose: () => void
  onDone: (label: string) => void
}) {
  const [artist, setArtist] = useState('')
  const [title, setTitle] = useState('')
  const [album, setAlbum] = useState('')
  const [provider, setProvider] = useState('ytdlp')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [results, setResults] = useState<
    { url: string; title: string; uploader: string; duration: number; score: number }[] | null
  >(null)

  async function preview() {
    setBusy(true)
    setError('')
    try {
      setResults(
        await api.searchDownloadable({ query: `${artist} ${title}`.trim(), artist, title }),
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Search failed')
    } finally {
      setBusy(false)
    }
  }

  async function submit() {
    setBusy(true)
    setError('')
    try {
      await api.createWanted({ artist: artist.trim(), title, album, provider })
      onDone(title || artist)
      setArtist('')
      setTitle('')
      setAlbum('')
      setResults(null)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not add that item')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title="Add to wanted"
      onClose={onClose}
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn-outline" onClick={preview} disabled={busy || !artist.trim()}>
            {busy && <Spinner className="h-4 w-4" />} Preview sources
          </button>
          <button className="btn-primary" onClick={submit} disabled={busy || !artist.trim()} data-testid="submit-wanted">
            Add
          </button>
        </>
      }
    >
      <ErrorBanner message={error} onDismiss={() => setError('')} />
      <div className="space-y-4">
        <div>
          <label className="label" htmlFor="w-artist">
            Artist
          </label>
          <input
            id="w-artist"
            className="input"
            value={artist}
            onChange={(e) => setArtist(e.target.value)}
            autoFocus
            data-testid="wanted-artist"
          />
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="label" htmlFor="w-title">
              Track <span className="normal-case text-subtle">(optional)</span>
            </label>
            <input id="w-title" className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="w-album">
              Album <span className="normal-case text-subtle">(optional)</span>
            </label>
            <input id="w-album" className="input" value={album} onChange={(e) => setAlbum(e.target.value)} />
          </div>
        </div>
        <div>
          <label className="label" htmlFor="w-provider">
            Fetch with
          </label>
          <select id="w-provider" className="input" value={provider} onChange={(e) => setProvider(e.target.value)}>
            <option value="ytdlp">yt-dlp (direct download)</option>
            <option value="lidarr">Lidarr (indexers / torrents)</option>
          </select>
        </div>

        {results && (
          <div className="rounded-lg border border-line bg-elevated p-3">
            <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-subtle">
              What yt-dlp would find
            </h4>
            {results.length === 0 ? (
              <p className="text-sm text-muted">No candidates found for that query.</p>
            ) : (
              <ul className="space-y-1.5 text-sm">
                {results.slice(0, 5).map((result) => (
                  <li key={result.url} className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="truncate text-zinc-100">{result.title}</p>
                      <p className="truncate text-xs text-subtle">{result.uploader}</p>
                    </div>
                    <span className="chip shrink-0">{Math.round(result.score * 100)}%</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </Modal>
  )
}
