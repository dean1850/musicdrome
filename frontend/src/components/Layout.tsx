import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '../store/auth'
import {
  Chart,
  Compass,
  Cog,
  Disc,
  Home,
  List,
  Logout,
  Mic,
  Note,
  Radio,
  Search as SearchIcon,
  Shield,
  Sparkles,
} from './icons'
import Player from './Player'

const NAV = [
  { to: '/', label: 'Home', icon: Home, end: true },
  { to: '/albums', label: 'Albums', icon: Disc },
  { to: '/artists', label: 'Artists', icon: Mic },
  { to: '/tracks', label: 'Tracks', icon: Note },
  { to: '/playlists', label: 'Playlists', icon: List },
  { to: '/discover', label: 'Discover', icon: Compass },
  { to: '/podcasts', label: 'Podcasts', icon: Radio },
  { to: '/analytics', label: 'Analytics', icon: Chart },
]

export default function Layout() {
  const { user, server, logout } = useAuth()
  const navigate = useNavigate()
  const [query, setQuery] = useState('')

  // Focus the search box on "/" the way most media apps do
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (target && ['INPUT', 'TEXTAREA'].includes(target.tagName)) return
      if (event.key === '/') {
        event.preventDefault()
        document.getElementById('global-search')?.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  function submitSearch(event: React.FormEvent) {
    event.preventDefault()
    if (query.trim()) navigate(`/search?q=${encodeURIComponent(query.trim())}`)
  }

  const linkClass = ({ isActive }: { isActive: boolean }) =>
    `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
      isActive ? 'bg-elevated font-medium text-white' : 'text-muted hover:bg-elevated hover:text-zinc-100'
    }`

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-1 overflow-hidden pb-20">
        {/* Sidebar */}
        <nav
          className="hidden w-60 shrink-0 flex-col border-r border-line bg-surface md:flex"
          data-testid="sidebar"
        >
          <Link to="/" className="flex items-center gap-2.5 px-5 py-5">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent">
              <Sparkles className="h-4 w-4 text-white" />
            </span>
            <span className="text-lg font-semibold tracking-tight text-white">Musicdrome</span>
          </Link>

          <div className="flex-1 space-y-0.5 overflow-y-auto px-3 pb-4">
            {NAV.map(({ to, label, icon: Icon, end }) => (
              <NavLink key={to} to={to} end={end} className={linkClass} data-testid={`nav-${label.toLowerCase()}`}>
                <Icon className="h-[18px] w-[18px]" />
                {label}
              </NavLink>
            ))}

            <div className="!mt-6 space-y-0.5 border-t border-line pt-4">
              <NavLink to="/settings" className={linkClass} data-testid="nav-settings">
                <Cog className="h-[18px] w-[18px]" />
                Settings
              </NavLink>
              {user?.is_admin && (
                <NavLink to="/admin" className={linkClass} data-testid="nav-admin">
                  <Shield className="h-[18px] w-[18px]" />
                  Admin
                </NavLink>
              )}
            </div>
          </div>

          <div className="border-t border-line px-3 py-3">
            <div className="flex items-center gap-3 rounded-lg px-2 py-1.5">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent/20 text-xs font-semibold uppercase text-accent-soft">
                {user?.username.slice(0, 2)}
              </span>
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm text-zinc-200" data-testid="current-user">
                  {user?.username}
                </div>
                <div className="truncate text-xs text-subtle">
                  {user?.is_admin ? 'Administrator' : 'Listener'}
                </div>
              </div>
              <button
                onClick={logout}
                className="text-subtle hover:text-white"
                aria-label="Sign out"
                data-testid="logout"
              >
                <Logout className="h-4 w-4" />
              </button>
            </div>
            {server && (
              <p className="px-2 pt-2 text-[10px] text-subtle">
                v{server.version} · Subsonic {server.subsonic_version}
              </p>
            )}
          </div>
        </nav>

        {/* Main column */}
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-30 flex items-center gap-3 border-b border-line bg-base/90 px-4 py-3 backdrop-blur md:px-8">
            <form onSubmit={submitSearch} className="relative w-full max-w-md">
              <SearchIcon className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-subtle" />
              <input
                id="global-search"
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search artists, albums, tracks…  (press /)"
                className="input pl-9"
                data-testid="global-search"
              />
            </form>

            {/* Mobile nav */}
            <nav className="ml-auto flex gap-1 md:hidden">
              {NAV.slice(0, 5).map(({ to, label, icon: Icon, end }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={end}
                  className={({ isActive }) =>
                    `rounded-lg p-2 ${isActive ? 'bg-elevated text-white' : 'text-muted'}`
                  }
                  aria-label={label}
                >
                  <Icon className="h-5 w-5" />
                </NavLink>
              ))}
            </nav>
          </header>

          <main className="flex-1 overflow-y-auto px-4 py-6 md:px-8" data-testid="main-content">
            <Outlet />
          </main>
        </div>
      </div>

      <Player />
    </div>
  )
}
