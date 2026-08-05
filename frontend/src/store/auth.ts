import { create } from 'zustand'
import { api, tokens } from '../lib/api'
import type { ServerInfo, User } from '../types'

interface AuthState {
  user: User | null
  server: ServerInfo | null
  loading: boolean
  error: string
  login: (username: string, password: string) => Promise<void>
  register: (username: string, password: string, email?: string) => Promise<void>
  logout: () => void
  bootstrap: () => Promise<void>
  refreshUser: () => Promise<void>
}

export const useAuth = create<AuthState>((set, get) => ({
  user: null,
  server: null,
  loading: true,
  error: '',

  async login(username, password) {
    set({ error: '' })
    const result = await api.login(username, password)
    tokens.set(result.access_token, result.refresh_token)
    const [user, server] = await Promise.all([api.me(), api.serverInfo()])
    set({ user, server })
  },

  async register(username, password, email) {
    set({ error: '' })
    const result = await api.register(username, password, email)
    tokens.set(result.access_token, result.refresh_token)
    const [user, server] = await Promise.all([api.me(), api.serverInfo()])
    set({ user, server })
  },

  logout() {
    tokens.clear()
    set({ user: null })
  },

  async bootstrap() {
    if (!tokens.access) {
      set({ loading: false, user: null })
      return
    }
    try {
      const [user, server] = await Promise.all([api.me(), api.serverInfo()])
      set({ user, server, loading: false })
    } catch {
      tokens.clear()
      set({ user: null, loading: false })
    }
  },

  async refreshUser() {
    if (!get().user) return
    try {
      set({ user: await api.me() })
    } catch {
      /* keep the stale user rather than flashing the login screen */
    }
  },
}))

// The API client fires this when a refresh attempt fails for good.
window.addEventListener('musicdrome:signed-out', () => {
  useAuth.setState({ user: null })
})
