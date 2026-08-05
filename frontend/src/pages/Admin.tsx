import { useState } from 'react'
import { Check, Plus, Refresh, Trash, X } from '../components/icons'
import { ErrorBanner, Loading, Modal, Spinner, StatTile, Tabs, Toast, Toggle } from '../components/ui'
import { api } from '../lib/api'
import { count, date, relativeTime } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { useAuth } from '../store/auth'

export default function Admin() {
  const [tab, setTab] = useState<'library' | 'users' | 'jobs' | 'integrations'>('library')
  const [toast, setToast] = useState('')

  return (
    <div className="space-y-5" data-testid="admin-page">
      <header>
        <h1 className="page-title">Administration</h1>
        <p className="mt-1 text-sm text-muted">Server-wide settings and maintenance.</p>
      </header>

      <Tabs
        value={tab}
        onChange={setTab}
        options={[
          { value: 'library', label: 'Library' },
          { value: 'users', label: 'Users' },
          { value: 'jobs', label: 'Jobs' },
          { value: 'integrations', label: 'Integrations' },
        ]}
      />

      {tab === 'library' && <LibraryTab onToast={setToast} />}
      {tab === 'users' && <UsersTab onToast={setToast} />}
      {tab === 'jobs' && <JobsTab onToast={setToast} />}
      {tab === 'integrations' && <IntegrationsTab onToast={setToast} />}

      <Toast message={toast} onDone={() => setToast('')} />
    </div>
  )
}

function LibraryTab({ onToast }: { onToast: (message: string) => void }) {
  const { data, loading, reload } = useAsync(() => api.scanStatus(), [])
  const { data: stats } = useAsync(() => api.stats(), [])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function scan(full: boolean) {
    setBusy(true)
    setError('')
    try {
      const result = await api.startScan(full)
      onToast(result.message)
      // Poll a couple of times so the progress figures move without a manual reload
      setTimeout(() => void reload(), 1500)
      setTimeout(() => void reload(), 6000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start the scan')
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <Loading />

  const scanning = data?.state?.scanning
  const progress =
    scanning && data?.state?.total
      ? Math.round((data.state.count / Math.max(data.state.total, 1)) * 100)
      : 0

  return (
    <div className="space-y-5">
      <ErrorBanner message={error} onDismiss={() => setError('')} />

      {stats && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile label="Tracks" value={count(stats.tracks)} />
          <StatTile label="Albums" value={count(stats.albums)} />
          <StatTile label="Artists" value={count(stats.artists)} />
          <StatTile label="Library size" value={`${(stats.size / 1024 ** 3).toFixed(1)} GB`} />
        </div>
      )}

      <section className="card p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-white">Library scan</h2>
            <p className="mt-1 break-all text-sm text-muted">
              Scanning <code className="text-zinc-300">{data?.music_dir}</code>
            </p>
            {data?.last_run && (
              <p className="mt-2 text-xs text-subtle">
                Last run {relativeTime(data.last_run.finished_at || data.last_run.started_at)} ·{' '}
                {count(data.last_run.tracks_added)} added, {count(data.last_run.tracks_updated)}{' '}
                updated, {count(data.last_run.tracks_removed)} removed
                {data.last_run.error ? ` · error: ${data.last_run.error}` : ''}
              </p>
            )}
          </div>
          <div className="flex gap-2">
            <button className="btn-outline" onClick={() => scan(false)} disabled={busy || scanning} data-testid="scan-quick">
              {busy && <Spinner className="h-4 w-4" />} Scan
            </button>
            <button className="btn-outline" onClick={() => scan(true)} disabled={busy || scanning} data-testid="scan-full">
              Full rescan
            </button>
            <button className="btn-ghost" onClick={() => void reload()} aria-label="Reload status">
              <Refresh className="h-4 w-4" />
            </button>
          </div>
        </div>

        {scanning && (
          <div className="mt-4" data-testid="scan-progress">
            <div className="mb-1 flex justify-between text-xs text-muted">
              <span>
                Scanning… {count(data.state.count)} / {count(data.state.total)}
              </span>
              <span>{progress}%</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-elevated">
              <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${progress}%` }} />
            </div>
          </div>
        )}
      </section>
    </div>
  )
}

function UsersTab({ onToast }: { onToast: (message: string) => void }) {
  const { user: me } = useAuth()
  const { data: users, loading, reload } = useAsync(() => api.users(), [])
  const [addOpen, setAddOpen] = useState(false)
  const [error, setError] = useState('')
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null)

  async function update(id: number, payload: Record<string, unknown>, message: string) {
    setError('')
    try {
      await api.updateUser(id, payload)
      onToast(message)
      void reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not update the account')
    }
  }

  if (loading) return <Loading />

  return (
    <div className="space-y-4">
      <ErrorBanner message={error} onDismiss={() => setError('')} />

      <div className="flex justify-end">
        <button className="btn-primary" onClick={() => setAddOpen(true)} data-testid="add-user">
          <Plus className="h-4 w-4" /> New user
        </button>
      </div>

      <ul className="space-y-2">
        {(users || []).map((user) => (
          <li key={user.id} className="card flex flex-wrap items-center gap-4 px-4 py-3" data-testid="user-row">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="truncate font-medium text-zinc-100">{user.username}</span>
                {user.is_admin && <span className="chip text-accent-soft">admin</span>}
                {!user.is_active && <span className="chip text-red-300">disabled</span>}
                {user.id === me?.id && <span className="chip">you</span>}
              </div>
              <p className="truncate text-xs text-subtle">
                {user.email || 'no email'} · joined {date(user.created_at)} · last seen{' '}
                {relativeTime(user.last_login_at)}
              </p>
            </div>

            <label className="flex items-center gap-2 text-xs text-muted">
              <Toggle
                checked={user.is_admin}
                onChange={(value) => update(user.id, { is_admin: value }, `Updated ${user.username}`)}
                label="Administrator"
              />
              Admin
            </label>
            <label className="flex items-center gap-2 text-xs text-muted">
              <Toggle
                checked={user.is_active}
                onChange={(value) => update(user.id, { is_active: value }, `Updated ${user.username}`)}
                label="Active"
              />
              Active
            </label>

            <button
              className="btn-ghost px-2 py-1 text-subtle hover:text-red-400 disabled:opacity-30"
              disabled={user.id === me?.id}
              onClick={() => setConfirmDelete(user.id)}
              aria-label={`Delete ${user.username}`}
            >
              <Trash className="h-4 w-4" />
            </button>
          </li>
        ))}
      </ul>

      <NewUserModal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        onDone={(username) => {
          onToast(`Created ${username}`)
          void reload()
        }}
      />

      <Modal
        open={confirmDelete !== null}
        title="Delete user"
        onClose={() => setConfirmDelete(null)}
        footer={
          <>
            <button className="btn-ghost" onClick={() => setConfirmDelete(null)}>
              Cancel
            </button>
            <button
              className="btn-danger"
              onClick={async () => {
                if (confirmDelete === null) return
                await api.deleteUser(confirmDelete)
                setConfirmDelete(null)
                onToast('User deleted')
                void reload()
              }}
            >
              Delete
            </button>
          </>
        }
      >
        <p className="text-sm text-muted">
          This removes the account along with its playlists, ratings and listening history. Files in
          the music library are untouched.
        </p>
      </Modal>
    </div>
  )
}

function NewUserModal({
  open,
  onClose,
  onDone,
}: {
  open: boolean
  onClose: () => void
  onDone: (username: string) => void
}) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [email, setEmail] = useState('')
  const [isAdmin, setIsAdmin] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit() {
    setBusy(true)
    setError('')
    try {
      await api.createUser({ username: username.trim(), password, email: email || undefined, is_admin: isAdmin })
      onDone(username.trim())
      setUsername('')
      setPassword('')
      setEmail('')
      setIsAdmin(false)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create the account')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title="New user"
      onClose={onClose}
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-primary"
            onClick={submit}
            disabled={busy || !username.trim() || password.length < 8}
            data-testid="create-user"
          >
            {busy && <Spinner className="h-4 w-4" />} Create
          </button>
        </>
      }
    >
      <ErrorBanner message={error} onDismiss={() => setError('')} />
      <div className="space-y-4">
        <div>
          <label className="label" htmlFor="new-username">
            Username
          </label>
          <input
            id="new-username"
            className="input"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            data-testid="new-username"
          />
        </div>
        <div>
          <label className="label" htmlFor="new-password">
            Password
          </label>
          <input
            id="new-password"
            type="password"
            className="input"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            data-testid="new-password"
          />
          <p className="mt-1 text-xs text-subtle">At least 8 characters.</p>
        </div>
        <div>
          <label className="label" htmlFor="new-email">
            Email <span className="normal-case text-subtle">(optional)</span>
          </label>
          <input id="new-email" type="email" className="input" value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        <label className="flex items-center gap-3 text-sm text-muted">
          <Toggle checked={isAdmin} onChange={setIsAdmin} label="Administrator" />
          Administrator
        </label>
      </div>
    </Modal>
  )
}

function JobsTab({ onToast }: { onToast: (message: string) => void }) {
  const { data: jobs, loading, reload } = useAsync(() => api.jobs(), [])

  if (loading) return <Loading />

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted">
        Background tasks run inside the server process. Trigger one now if you don't want to wait
        for its next slot.
      </p>

      <ul className="space-y-2">
        {(jobs || []).map((job) => (
          <li key={job.id} className="card flex flex-wrap items-center gap-3 px-4 py-3" data-testid="job-row">
            <div className="min-w-0 flex-1">
              <h3 className="font-mono text-sm text-zinc-100">{job.id}</h3>
              <p className="truncate text-xs text-subtle">{job.trigger}</p>
            </div>
            <span className="text-xs text-muted">
              next {job.next_run ? relativeTime(job.next_run).replace('ago', 'from now') : 'not scheduled'}
            </span>
            <button
              className="btn-outline px-2.5 py-1 text-xs"
              onClick={async () => {
                await api.runJob(job.id)
                onToast(`Running ${job.id}`)
                setTimeout(() => void reload(), 1200)
              }}
              data-testid="run-job"
            >
              Run now
            </button>
          </li>
        ))}
        {!jobs?.length && <li className="text-sm text-subtle">No jobs scheduled.</li>}
      </ul>
    </div>
  )
}

function IntegrationsTab({ onToast }: { onToast: (message: string) => void }) {
  const { data, loading } = useAsync(() => api.integrations(), [])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [lidarrInfo, setLidarrInfo] = useState<any>(null)

  if (loading) return <Loading />
  if (!data) return null

  const rows: { name: string; ok: boolean; detail: string }[] = [
    {
      name: 'ffmpeg (transcoding)',
      ok: data.ffmpeg.available,
      detail: data.ffmpeg.available ? data.ffmpeg.path : `not found at ${data.ffmpeg.path}`,
    },
    {
      name: 'Last.fm',
      ok: data.lastfm.configured,
      detail: data.lastfm.can_scrobble
        ? 'metadata + scrobbling'
        : data.lastfm.configured
          ? 'metadata only (no API secret)'
          : 'no API key set',
    },
    { name: 'ListenBrainz', ok: data.listenbrainz.enabled, detail: data.listenbrainz.api_url },
    {
      name: 'MusicBrainz',
      ok: data.musicbrainz.enabled,
      detail: `${data.musicbrainz.rate_limit}s between requests`,
    },
    {
      name: `AI (${data.ai.provider})`,
      ok: Boolean(data.ai.enabled && data.ai.providers?.[data.ai.provider]?.available),
      detail: data.ai.providers?.[data.ai.provider]?.model || 'not configured',
    },
    {
      name: 'Acquisition (yt-dlp)',
      ok: data.acquisition.enabled,
      detail: `${data.acquisition.auto_download ? 'automatic' : 'approval required'} · max ${data.acquisition.max_per_day}/day`,
    },
    { name: 'Lidarr', ok: data.lidarr.configured, detail: data.lidarr.url },
  ]

  return (
    <div className="space-y-4">
      <ErrorBanner message={error} onDismiss={() => setError('')} />

      <ul className="space-y-2">
        {rows.map((row) => (
          <li key={row.name} className="card flex items-center gap-3 px-4 py-3" data-testid="integration-row">
            <span className={row.ok ? 'text-emerald-400' : 'text-subtle'}>
              {row.ok ? <Check className="h-4 w-4" /> : <X className="h-4 w-4" />}
            </span>
            <div className="min-w-0 flex-1">
              <h3 className="text-sm font-medium text-zinc-100">{row.name}</h3>
              <p className="truncate text-xs text-subtle">{row.detail}</p>
            </div>
          </li>
        ))}
      </ul>

      {data.lidarr.enabled && (
        <section className="card p-5">
          <h2 className="text-sm font-semibold text-white">Lidarr</h2>
          <p className="mt-1 text-sm text-muted">
            Musicdrome pushes wanted artists into Lidarr and imports what Lidarr grabs.
          </p>
          <div className="mt-4 flex gap-2">
            <button
              className="btn-outline"
              disabled={busy === 'test'}
              onClick={async () => {
                setBusy('test')
                setError('')
                try {
                  setLidarrInfo(await api.testLidarr())
                } catch (err) {
                  setError(err instanceof Error ? err.message : 'Lidarr test failed')
                } finally {
                  setBusy('')
                }
              }}
              data-testid="test-lidarr"
            >
              {busy === 'test' && <Spinner className="h-4 w-4" />} Test connection
            </button>
            <button
              className="btn-outline"
              disabled={busy === 'sync'}
              onClick={async () => {
                setBusy('sync')
                setError('')
                try {
                  const result = await api.syncLidarr()
                  onToast(result.message)
                } catch (err) {
                  setError(err instanceof Error ? err.message : 'Lidarr sync failed')
                } finally {
                  setBusy('')
                }
              }}
            >
              {busy === 'sync' && <Spinner className="h-4 w-4" />} Sync now
            </button>
          </div>

          {lidarrInfo && (
            <pre className="mt-4 overflow-x-auto rounded-lg bg-elevated p-3 text-xs text-muted">
              {JSON.stringify(lidarrInfo, null, 2)}
            </pre>
          )}
        </section>
      )}
    </div>
  )
}
