import { useRef, useState } from 'react'
import { Grid, PlaylistCard } from '../components/Cards'
import { List, Plus, Refresh, Sparkles, Upload } from '../components/icons'
import { EmptyState, ErrorBanner, Loading, Modal, Spinner, Tabs, Toast } from '../components/ui'
import { api } from '../lib/api'
import { useAsync } from '../lib/hooks'
import { useAuth } from '../store/auth'

type Kind = 'all' | 'manual' | 'smart' | 'ai' | 'imported'

export default function Playlists() {
  const { user, server } = useAuth()
  const [kind, setKind] = useState<Kind>('all')
  const [toast, setToast] = useState('')
  const [newOpen, setNewOpen] = useState(false)
  const [aiOpen, setAiOpen] = useState(false)
  const [smartOpen, setSmartOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)

  const { data: playlists, loading, error, reload } = useAsync(() => api.playlists(kind), [kind])

  const aiAvailable = Boolean(server?.features?.ai) && Boolean(user?.ai_enabled)

  return (
    <div className="space-y-5" data-testid="playlists-page">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="page-title">Playlists</h1>
        <div className="flex flex-wrap gap-2">
          <button className="btn-outline" onClick={() => setNewOpen(true)} data-testid="new-playlist">
            <Plus className="h-4 w-4" /> New playlist
          </button>
          <button className="btn-outline" onClick={() => setSmartOpen(true)} data-testid="new-smart">
            <List className="h-4 w-4" /> New smart playlist
          </button>
          <button className="btn-outline" onClick={() => setImportOpen(true)} data-testid="import-m3u">
            <Upload className="h-4 w-4" /> Import M3U
          </button>
          <button
            className="btn-primary"
            onClick={() => setAiOpen(true)}
            disabled={!aiAvailable}
            title={aiAvailable ? undefined : 'Configure an AI provider in .env to enable this'}
            data-testid="new-ai"
          >
            <Sparkles className="h-4 w-4" /> Curate with AI
          </button>
        </div>
      </header>

      <Tabs
        value={kind}
        onChange={setKind}
        options={[
          { value: 'all', label: 'All' },
          { value: 'manual', label: 'Manual' },
          { value: 'smart', label: 'Smart' },
          { value: 'ai', label: 'AI' },
          { value: 'imported', label: 'Imported' },
        ]}
      />

      <ErrorBanner message={error} />

      {loading ? (
        <Loading label="Loading playlists" />
      ) : !playlists?.length ? (
        <EmptyState
          title="No playlists here"
          description="Build one by hand, define rules for a smart playlist, or let the AI curate one from your library."
          action={
            <button className="btn-primary" onClick={() => setNewOpen(true)}>
              <Plus className="h-4 w-4" /> New playlist
            </button>
          }
        />
      ) : (
        <Grid>
          {playlists.map((playlist) => (
            <PlaylistCard key={playlist.id} playlist={playlist} />
          ))}
        </Grid>
      )}

      <NewPlaylistModal
        open={newOpen}
        onClose={() => setNewOpen(false)}
        onDone={(name) => {
          setToast(`Created "${name}"`)
          void reload()
        }}
      />
      <SmartPlaylistModal
        open={smartOpen}
        onClose={() => setSmartOpen(false)}
        onDone={(name) => {
          setToast(`Created smart playlist "${name}"`)
          void reload()
        }}
      />
      <AIPlaylistModal
        open={aiOpen}
        onClose={() => setAiOpen(false)}
        onDone={(name) => {
          setToast(`Curated "${name}"`)
          void reload()
        }}
      />
      <ImportModal
        open={importOpen}
        isAdmin={Boolean(user?.is_admin)}
        onClose={() => setImportOpen(false)}
        onDone={(message) => {
          setToast(message)
          void reload()
        }}
      />

      <Toast message={toast} onDone={() => setToast('')} />
    </div>
  )
}

function NewPlaylistModal({
  open,
  onClose,
  onDone,
}: {
  open: boolean
  onClose: () => void
  onDone: (name: string) => void
}) {
  const [name, setName] = useState('')
  const [comment, setComment] = useState('')
  const [isPublic, setIsPublic] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit() {
    if (!name.trim()) return
    setBusy(true)
    setError('')
    try {
      await api.createPlaylist({ name: name.trim(), comment, public: isPublic })
      onDone(name.trim())
      setName('')
      setComment('')
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create the playlist')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title="New playlist"
      onClose={onClose}
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn-primary" onClick={submit} disabled={busy || !name.trim()} data-testid="create-playlist">
            {busy && <Spinner className="h-4 w-4" />} Create
          </button>
        </>
      }
    >
      <ErrorBanner message={error} />
      <div className="space-y-4">
        <div>
          <label className="label" htmlFor="pl-name">
            Name
          </label>
          <input
            id="pl-name"
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
            data-testid="playlist-name"
          />
        </div>
        <div>
          <label className="label" htmlFor="pl-comment">
            Description
          </label>
          <input id="pl-comment" className="input" value={comment} onChange={(e) => setComment(e.target.value)} />
        </div>
        <label className="flex items-center gap-2 text-sm text-muted">
          <input
            type="checkbox"
            checked={isPublic}
            onChange={(e) => setIsPublic(e.target.checked)}
            className="h-4 w-4 rounded border-line bg-elevated"
          />
          Visible to other users on this server
        </label>
      </div>
    </Modal>
  )
}

function ImportModal({
  open,
  isAdmin,
  onClose,
  onDone,
}: {
  open: boolean
  isAdmin: boolean
  onClose: () => void
  onDone: (message: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)

  // Only fetched for admins — it walks the import roots to count files.
  const { data: status, reload: reloadStatus } = useAsync(
    () => (open && isAdmin ? api.importStatus() : Promise.resolve(null)),
    [open, isAdmin],
  )

  async function scanNow(force: boolean) {
    setBusy(true)
    setError('')
    try {
      const result = await api.importPlaylists(force)
      if (!result.ok) setError(result.message)
      else onDone(result.message)
      void reloadStatus()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The import failed')
    } finally {
      setBusy(false)
    }
  }

  async function upload(file: File) {
    setBusy(true)
    setError('')
    try {
      const playlist = await api.uploadPlaylist(file)
      onDone(
        playlist.import_missing
          ? `Imported "${playlist.name}" — ${playlist.import_missing} entries are not in your library`
          : `Imported "${playlist.name}"`,
      )
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not read that file')
    } finally {
      setBusy(false)
      if (fileInput.current) fileInput.current.value = ''
    }
  }

  return (
    <Modal
      open={open}
      title="Import M3U playlists"
      onClose={onClose}
      wide
      footer={
        <button className="btn-ghost" onClick={onClose}>
          Close
        </button>
      }
    >
      <ErrorBanner message={error} onDismiss={() => setError('')} />

      <div className="space-y-6">
        <section>
          <h3 className="text-sm font-semibold text-white">Automatic</h3>
          <p className="mt-1 text-sm text-muted">
            Any <code className="text-zinc-200">.m3u</code> or{' '}
            <code className="text-zinc-200">.m3u8</code> file inside your library is imported on
            its own — within seconds of a downloader writing it, and again on every library scan.
            Editing the file re-syncs the playlist; deleting it removes the playlist.
          </p>

          {isAdmin && status && (
            <div className="mt-3 space-y-2 rounded-lg border border-line bg-elevated p-3 text-xs">
              {status.roots.map((root) => (
                <div key={root.path} className="flex items-center justify-between gap-3">
                  <code className="truncate text-zinc-300" title={root.path}>
                    {root.path}
                  </code>
                  <span className={root.exists ? 'shrink-0 text-subtle' : 'shrink-0 text-red-300'}>
                    {root.exists ? `${root.files} file(s)` : 'not mounted'}
                  </span>
                </div>
              ))}
              {status.last_run.at && (
                <p className="border-t border-line pt-2 text-subtle">
                  Last pass: {status.last_run.created} imported, {status.last_run.updated} updated,{' '}
                  {status.last_run.deleted} removed, {status.last_run.missing} entries not in the
                  library
                </p>
              )}
            </div>
          )}

          {isAdmin && (
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                className="btn-outline"
                onClick={() => scanNow(false)}
                disabled={busy}
                data-testid="import-scan"
              >
                {busy ? <Spinner className="h-4 w-4" /> : <Refresh className="h-4 w-4" />} Scan now
              </button>
              <button className="btn-ghost" onClick={() => scanNow(true)} disabled={busy}>
                Re-read every file
              </button>
            </div>
          )}
        </section>

        <section className="border-t border-line pt-5">
          <h3 className="text-sm font-semibold text-white">Upload a file</h3>
          <p className="mt-1 text-sm text-muted">
            For a playlist that lives somewhere Musicdrome cannot see. Its entries are matched
            against your library by path and by artist and title; the result is a normal playlist
            you own, not tied to the file.
          </p>
          <input
            ref={fileInput}
            type="file"
            accept=".m3u,.m3u8,audio/x-mpegurl,audio/mpegurl"
            disabled={busy}
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) void upload(file)
            }}
            data-testid="import-upload"
            className="mt-3 block w-full cursor-pointer rounded-lg border border-line bg-elevated text-sm text-muted file:mr-3 file:cursor-pointer file:rounded-l-lg file:border-0 file:bg-surface file:px-4 file:py-2.5 file:text-sm file:text-zinc-100 hover:file:bg-line"
          />
        </section>
      </div>
    </Modal>
  )
}

function SmartPlaylistModal({
  open,
  onClose,
  onDone,
}: {
  open: boolean
  onClose: () => void
  onDone: (name: string) => void
}) {
  const { data: templates } = useAsync(() => api.smartTemplates(), [])
  const [name, setName] = useState('')
  const [rules, setRules] = useState('{\n  "all": [\n    {"gt": {"playCount": 3}}\n  ],\n  "sort": "playCount",\n  "order": "desc",\n  "limit": 50\n}')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit() {
    setBusy(true)
    setError('')
    try {
      const parsed = JSON.parse(rules)
      await api.createSmartPlaylist({ name: name.trim(), rules: parsed })
      onDone(name.trim())
      onClose()
    } catch (err) {
      setError(
        err instanceof SyntaxError
          ? `The rule document is not valid JSON: ${err.message}`
          : err instanceof Error
            ? err.message
            : 'Could not create the playlist',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title="New smart playlist"
      onClose={onClose}
      wide
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn-primary" onClick={submit} disabled={busy || !name.trim()} data-testid="create-smart">
            {busy && <Spinner className="h-4 w-4" />} Create
          </button>
        </>
      }
    >
      <ErrorBanner message={error} />
      <div className="space-y-4">
        <div>
          <label className="label" htmlFor="smart-name">
            Name
          </label>
          <input
            id="smart-name"
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
            data-testid="smart-name"
          />
        </div>

        {templates && templates.length > 0 && (
          <div>
            <span className="label">Start from a template</span>
            <div className="flex flex-wrap gap-2">
              {templates.map((template: any) => (
                <button
                  key={template.name}
                  type="button"
                  className="chip hover:border-accent hover:text-accent-soft"
                  onClick={() => {
                    setName(template.name)
                    setRules(JSON.stringify(template.rules, null, 2))
                  }}
                >
                  {template.name}
                </button>
              ))}
            </div>
          </div>
        )}

        <div>
          <label className="label" htmlFor="smart-rules">
            Rules
          </label>
          <textarea
            id="smart-rules"
            className="input h-56 font-mono text-xs"
            value={rules}
            onChange={(e) => setRules(e.target.value)}
            spellCheck={false}
            data-testid="smart-rules"
          />
          <p className="mt-2 text-xs text-subtle">
            Fields: title, artist, album, genre, year, playCount, lastPlayed, rating, starred,
            dateAdded, bitrate, duration. Operators: is, isNot, gt, lt, gte, lte, contains,
            startsWith, inTheRange, inTheLast, notInTheLast, isNull. Combine with all / any / not.
          </p>
        </div>
      </div>
    </Modal>
  )
}

function AIPlaylistModal({
  open,
  onClose,
  onDone,
}: {
  open: boolean
  onClose: () => void
  onDone: (name: string) => void
}) {
  const [brief, setBrief] = useState('')
  const [maxTracks, setMaxTracks] = useState(30)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [preview, setPreview] = useState<Awaited<ReturnType<typeof api.previewAIPlaylist>> | null>(null)

  const SUGGESTIONS = [
    'A slow, warm mix for a rainy Sunday morning',
    'High-energy tracks for a long run',
    'Deep cuts I own but never play',
    'Instrumental focus music with no vocals',
  ]

  async function runPreview() {
    setBusy(true)
    setError('')
    try {
      setPreview(await api.previewAIPlaylist({ brief: brief.trim(), max_tracks: maxTracks }))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The AI provider could not be reached')
    } finally {
      setBusy(false)
    }
  }

  async function save() {
    setBusy(true)
    setError('')
    try {
      const playlist = await api.createAIPlaylist({ brief: brief.trim(), max_tracks: maxTracks })
      onDone(playlist.name)
      setPreview(null)
      setBrief('')
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
      title="Curate with AI"
      onClose={onClose}
      wide
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn-outline" onClick={runPreview} disabled={busy || brief.trim().length < 3}>
            {busy && <Spinner className="h-4 w-4" />} Preview
          </button>
          <button
            className="btn-primary"
            onClick={save}
            disabled={busy || brief.trim().length < 3}
            data-testid="create-ai-playlist"
          >
            {busy && <Spinner className="h-4 w-4" />} Create playlist
          </button>
        </>
      }
    >
      <ErrorBanner message={error} onDismiss={() => setError('')} />

      <div className="space-y-4">
        <div>
          <label className="label" htmlFor="ai-brief">
            What should this playlist feel like?
          </label>
          <textarea
            id="ai-brief"
            className="input h-24"
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
            placeholder="Describe the mood, activity or thread you want…"
            autoFocus
            data-testid="ai-brief"
          />
          <div className="mt-2 flex flex-wrap gap-2">
            {SUGGESTIONS.map((suggestion) => (
              <button
                key={suggestion}
                type="button"
                className="chip hover:border-accent hover:text-accent-soft"
                onClick={() => setBrief(suggestion)}
              >
                {suggestion}
              </button>
            ))}
          </div>
        </div>

        <div className="w-40">
          <label className="label" htmlFor="ai-max">
            Max tracks
          </label>
          <input
            id="ai-max"
            type="number"
            min={5}
            max={200}
            className="input"
            value={maxTracks}
            onChange={(e) => setMaxTracks(Number(e.target.value))}
          />
        </div>

        <p className="text-xs text-subtle">
          The model only picks from tracks already in your library — it never invents songs you
          don't own.
        </p>

        {preview && (
          <div className="rounded-lg border border-line bg-elevated p-4" data-testid="ai-preview">
            <h3 className="text-sm font-semibold text-white">{preview.name}</h3>
            <p className="mt-1 text-sm text-muted">{preview.description}</p>
            {preview.rationale && <p className="mt-2 text-xs italic text-subtle">{preview.rationale}</p>}
            <ul className="mt-3 max-h-56 space-y-1 overflow-y-auto text-sm">
              {preview.tracks.map((track, i) => (
                <li key={track.id} className="flex gap-2">
                  <span className="w-5 shrink-0 text-right text-xs tabular-nums text-subtle">{i + 1}</span>
                  <div className="min-w-0">
                    <span className="text-zinc-100">{track.title}</span>
                    <span className="text-subtle"> — {track.artist_name}</span>
                    {track.note && <p className="truncate text-xs italic text-subtle">{track.note}</p>}
                  </div>
                </li>
              ))}
            </ul>
            <p className="mt-3 text-[11px] text-subtle">Curated by {preview.model}</p>
          </div>
        )}
      </div>
    </Modal>
  )
}
