import { useState } from 'react'
import { Check, Sparkles, X } from '../components/icons'
import { ErrorBanner, Loading, Spinner, Toast, Toggle } from '../components/ui'
import { api } from '../lib/api'
import { useAsync } from '../lib/hooks'
import { useAuth } from '../store/auth'

const FORMATS = [
  { value: '', label: 'Original (no transcoding)' },
  { value: 'mp3', label: 'MP3' },
  { value: 'opus', label: 'Opus' },
  { value: 'aac', label: 'AAC' },
  { value: 'ogg', label: 'Ogg Vorbis' },
]

const BITRATES = [0, 96, 128, 192, 256, 320]

export default function Settings() {
  const { user, refreshUser } = useAuth()
  const [toast, setToast] = useState('')

  if (!user) return <Loading />

  return (
    <div className="max-w-3xl space-y-6" data-testid="settings-page">
      <header>
        <h1 className="page-title">Settings</h1>
        <p className="mt-1 text-sm text-muted">Signed in as {user.username}</p>
      </header>

      <Playback onSaved={(m) => { setToast(m); void refreshUser() }} />
      <Scrobbling onSaved={(m) => { setToast(m); void refreshUser() }} />
      <AISection onSaved={(m) => { setToast(m); void refreshUser() }} />
      <PasswordSection onSaved={setToast} />
      <ClientSection />

      <Toast message={toast} onDone={() => setToast('')} />
    </div>
  )
}

function Card({ title, description, children }: { title: string; description?: string; children: React.ReactNode }) {
  return (
    <section className="card p-5">
      <h2 className="text-sm font-semibold text-white">{title}</h2>
      {description && <p className="mt-1 text-sm text-muted">{description}</p>}
      <div className="mt-4">{children}</div>
    </section>
  )
}

function Playback({ onSaved }: { onSaved: (message: string) => void }) {
  const { user } = useAuth()
  const [format, setFormat] = useState(user?.transcode_format || '')
  const [bitrate, setBitrate] = useState(user?.max_bitrate ?? 0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function save() {
    setBusy(true)
    setError('')
    try {
      await api.updateMe({ transcode_format: format || null, max_bitrate: bitrate } as never)
      onSaved('Playback preferences saved')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card
      title="Playback"
      description="Applies to this account across the web player and any Subsonic client you sign in with."
    >
      <ErrorBanner message={error} onDismiss={() => setError('')} />
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label className="label" htmlFor="format">
            Preferred format
          </label>
          <select id="format" className="input" value={format} onChange={(e) => setFormat(e.target.value)}>
            {FORMATS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="bitrate">
            Maximum bitrate
          </label>
          <select
            id="bitrate"
            className="input"
            value={bitrate}
            onChange={(e) => setBitrate(Number(e.target.value))}
            data-testid="max-bitrate"
          >
            {BITRATES.map((value) => (
              <option key={value} value={value}>
                {value === 0 ? 'Unlimited' : `${value} kbps`}
              </option>
            ))}
          </select>
        </div>
      </div>
      <button className="btn-primary mt-4" onClick={save} disabled={busy} data-testid="save-playback">
        {busy && <Spinner className="h-4 w-4" />} Save
      </button>
    </Card>
  )
}

function Scrobbling({ onSaved }: { onSaved: (message: string) => void }) {
  const { user } = useAuth()
  const [lastfmUser, setLastfmUser] = useState('')
  const [lastfmPass, setLastfmPass] = useState('')
  const [lbToken, setLbToken] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  async function connectLastFm() {
    setBusy('lastfm')
    setError('')
    try {
      const result = await api.connectLastFm(lastfmUser, lastfmPass)
      onSaved(result.message)
      setLastfmUser('')
      setLastfmPass('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not connect to Last.fm')
    } finally {
      setBusy('')
    }
  }

  async function connectListenBrainz() {
    setBusy('lb')
    setError('')
    try {
      const result = await api.connectListenBrainz(lbToken)
      onSaved(result.message)
      setLbToken('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not connect to ListenBrainz')
    } finally {
      setBusy('')
    }
  }

  return (
    <Card title="Scrobbling" description="Send your plays to Last.fm and ListenBrainz.">
      <ErrorBanner message={error} onDismiss={() => setError('')} />

      <div className="space-y-5">
        <div>
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-medium text-zinc-200">Last.fm</h3>
            {user?.lastfm_enabled ? (
              <span className="flex items-center gap-1 text-xs text-emerald-300">
                <Check className="h-3.5 w-3.5" /> {user.lastfm_username}
              </span>
            ) : (
              <span className="text-xs text-subtle">Not connected</span>
            )}
          </div>
          {user?.lastfm_enabled ? (
            <button
              className="btn-outline"
              onClick={async () => {
                await api.disconnectLastFm()
                onSaved('Disconnected from Last.fm')
              }}
            >
              <X className="h-4 w-4" /> Disconnect
            </button>
          ) : (
            <div className="flex flex-wrap gap-2">
              <input
                className="input w-44"
                placeholder="Last.fm username"
                value={lastfmUser}
                onChange={(e) => setLastfmUser(e.target.value)}
              />
              <input
                className="input w-44"
                type="password"
                placeholder="Password"
                value={lastfmPass}
                onChange={(e) => setLastfmPass(e.target.value)}
              />
              <button
                className="btn-outline"
                onClick={connectLastFm}
                disabled={busy === 'lastfm' || !lastfmUser || !lastfmPass}
              >
                {busy === 'lastfm' && <Spinner className="h-4 w-4" />} Connect
              </button>
            </div>
          )}
          <p className="mt-1.5 text-xs text-subtle">
            Your password is exchanged for a session key and never stored.
          </p>
        </div>

        <div className="border-t border-line pt-5">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-medium text-zinc-200">ListenBrainz</h3>
            {user?.listenbrainz_enabled ? (
              <span className="flex items-center gap-1 text-xs text-emerald-300">
                <Check className="h-3.5 w-3.5" /> Connected
              </span>
            ) : (
              <span className="text-xs text-subtle">Not connected</span>
            )}
          </div>
          {user?.listenbrainz_enabled ? (
            <button
              className="btn-outline"
              onClick={async () => {
                await api.disconnectListenBrainz()
                onSaved('Disconnected from ListenBrainz')
              }}
            >
              <X className="h-4 w-4" /> Disconnect
            </button>
          ) : (
            <div className="flex flex-wrap gap-2">
              <input
                className="input w-96 max-w-full"
                placeholder="User token from listenbrainz.org/profile"
                value={lbToken}
                onChange={(e) => setLbToken(e.target.value)}
              />
              <button className="btn-outline" onClick={connectListenBrainz} disabled={busy === 'lb' || !lbToken}>
                {busy === 'lb' && <Spinner className="h-4 w-4" />} Connect
              </button>
            </div>
          )}
        </div>
      </div>
    </Card>
  )
}

function AISection({ onSaved }: { onSaved: (message: string) => void }) {
  const { user, server } = useAuth()
  const { data: status } = useAsync(() => api.aiStatus(), [])
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState('')
  const [error, setError] = useState('')

  const serverEnabled = Boolean(server?.features?.ai)

  return (
    <Card
      title="AI features"
      description="Powers curated playlists, listening reports and recommendations."
    >
      <ErrorBanner message={error} onDismiss={() => setError('')} />

      {!serverEnabled ? (
        <p className="text-sm text-muted">
          AI is disabled server-wide. Set <code className="text-zinc-300">AI_ENABLED=true</code> and
          configure a provider in <code className="text-zinc-300">.env</code>.
        </p>
      ) : (
        <div className="space-y-4">
          <label className="flex items-center justify-between gap-4">
            <span className="text-sm text-zinc-200">Use AI features on my account</span>
            <Toggle
              checked={Boolean(user?.ai_enabled)}
              onChange={async (value) => {
                await api.updateMe({ ai_enabled: value } as never)
                onSaved(value ? 'AI features enabled' : 'AI features disabled')
              }}
              label="AI features"
            />
          </label>

          {status && (
            <div className="rounded-lg border border-line bg-elevated px-4 py-3 text-sm">
              <div className="flex items-center gap-2">
                <Sparkles className="h-4 w-4 text-accent-soft" />
                <span className="text-zinc-200">
                  Provider: <span className="font-medium">{status.provider}</span>
                </span>
              </div>
              <ul className="mt-2 space-y-1 text-xs text-muted">
                {Object.entries(status.providers || {}).map(([name, details]: [string, any]) => (
                  <li key={name} className="flex items-center gap-2">
                    <span className={details.available ? 'text-emerald-400' : 'text-subtle'}>
                      {details.available ? '●' : '○'}
                    </span>
                    {name} — {details.model}
                    {!details.available && ' (not configured)'}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="flex items-center gap-3">
            <button
              className="btn-outline"
              disabled={testing}
              onClick={async () => {
                setTesting(true)
                setError('')
                setResult('')
                try {
                  const response = await api.aiTest()
                  setResult(`${response.message} — ${response.data?.model}`)
                } catch (err) {
                  setError(err instanceof Error ? err.message : 'Test failed')
                } finally {
                  setTesting(false)
                }
              }}
              data-testid="test-ai"
            >
              {testing && <Spinner className="h-4 w-4" />} Test connection
            </button>
            {result && <span className="text-sm text-emerald-300">{result}</span>}
          </div>
        </div>
      )}
    </Card>
  )
}

function PasswordSection({ onSaved }: { onSaved: (message: string) => void }) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit() {
    setBusy(true)
    setError('')
    try {
      await api.changePassword(current, next)
      onSaved('Password updated')
      setCurrent('')
      setNext('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not change the password')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title="Password">
      <ErrorBanner message={error} onDismiss={() => setError('')} />
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label className="label" htmlFor="current-pw">
            Current password
          </label>
          <input
            id="current-pw"
            type="password"
            className="input"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            autoComplete="current-password"
          />
        </div>
        <div>
          <label className="label" htmlFor="new-pw">
            New password
          </label>
          <input
            id="new-pw"
            type="password"
            className="input"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            autoComplete="new-password"
            minLength={8}
          />
        </div>
      </div>
      <button
        className="btn-primary mt-4"
        onClick={submit}
        disabled={busy || next.length < 8 || !current}
        data-testid="save-password"
      >
        {busy && <Spinner className="h-4 w-4" />} Change password
      </button>
      <p className="mt-2 text-xs text-subtle">
        Changing this also updates the credentials your Subsonic clients use.
      </p>
    </Card>
  )
}

function ClientSection() {
  const { user } = useAuth()
  const origin = typeof window !== 'undefined' ? window.location.origin : ''

  return (
    <Card
      title="Connect a Subsonic client"
      description="Musicdrome speaks the Subsonic API, so apps like Symfonium, substreamer, play:Sub, DSub and Feishin work out of the box."
    >
      <dl className="space-y-2 text-sm">
        <div className="flex gap-3">
          <dt className="w-24 shrink-0 text-subtle">Server</dt>
          <dd className="min-w-0 break-all font-mono text-zinc-200" data-testid="subsonic-url">
            {origin}
          </dd>
        </div>
        <div className="flex gap-3">
          <dt className="w-24 shrink-0 text-subtle">Username</dt>
          <dd className="font-mono text-zinc-200">{user?.username}</dd>
        </div>
        <div className="flex gap-3">
          <dt className="w-24 shrink-0 text-subtle">Password</dt>
          <dd className="text-muted">The same one you use here</dd>
        </div>
      </dl>
    </Card>
  )
}
