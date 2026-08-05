import { useState } from 'react'
import { useAsync } from '../lib/hooks'
import { useAuth } from '../store/auth'
import { Sparkles } from '../components/icons'
import { ErrorBanner, Spinner } from '../components/ui'

export default function Login() {
  const { login, register } = useAuth()
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [email, setEmail] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // Ask the server whether self-registration is even possible before offering it
  const { data: info } = useAsync(
    () => fetch('/api/v1/health').then((r) => r.json()).catch(() => null),
    [],
  )

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setError('')
    setBusy(true)
    try {
      if (mode === 'login') await login(username, password)
      else await register(username, password, email || undefined)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign-in failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center px-4 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <span className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-accent">
            <Sparkles className="h-6 w-6 text-white" />
          </span>
          <h1 className="text-2xl font-semibold tracking-tight text-white">Musicdrome</h1>
          <p className="mt-1 text-sm text-muted">
            {mode === 'login' ? 'Sign in to your library' : 'Create your account'}
          </p>
        </div>

        <form onSubmit={submit} className="card space-y-4 p-6" data-testid="login-form">
          <ErrorBanner message={error} onDismiss={() => setError('')} />

          <div>
            <label className="label" htmlFor="username">
              Username
            </label>
            <input
              id="username"
              className="input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoFocus
              required
              data-testid="username"
            />
          </div>

          {mode === 'register' && (
            <div>
              <label className="label" htmlFor="email">
                Email <span className="normal-case text-subtle">(optional)</span>
              </label>
              <input
                id="email"
                type="email"
                className="input"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
              />
            </div>
          )}

          <div>
            <label className="label" htmlFor="password">
              Password
            </label>
            <input
              id="password"
              type="password"
              className="input"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              minLength={mode === 'register' ? 8 : undefined}
              required
              data-testid="password"
            />
            {mode === 'register' && (
              <p className="mt-1 text-xs text-subtle">At least 8 characters.</p>
            )}
          </div>

          <button type="submit" className="btn-primary w-full" disabled={busy} data-testid="submit">
            {busy && <Spinner className="h-4 w-4" />}
            {mode === 'login' ? 'Sign in' : 'Create account'}
          </button>

          <p className="text-center text-xs text-subtle">
            {mode === 'login' ? (
              <>
                No account?{' '}
                <button
                  type="button"
                  onClick={() => {
                    setMode('register')
                    setError('')
                  }}
                  className="text-accent-soft hover:underline"
                  data-testid="switch-register"
                >
                  Register
                </button>
              </>
            ) : (
              <>
                Already have one?{' '}
                <button
                  type="button"
                  onClick={() => {
                    setMode('login')
                    setError('')
                  }}
                  className="text-accent-soft hover:underline"
                >
                  Sign in
                </button>
              </>
            )}
          </p>
        </form>

        {info && (
          <p className="mt-6 text-center text-xs text-subtle">
            Any Subsonic client can connect to this server with the same credentials.
          </p>
        )}
      </div>
    </div>
  )
}
