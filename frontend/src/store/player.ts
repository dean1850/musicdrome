import { create } from 'zustand'
import { api } from '../lib/api'
import type { Track } from '../types'

type RepeatMode = 'off' | 'all' | 'one'

interface PlayerState {
  queue: Track[]
  index: number
  playing: boolean
  position: number
  duration: number
  volume: number
  muted: boolean
  shuffle: boolean
  repeat: RepeatMode
  /** Play submitted for the current track, so we scrobble once per play. */
  scrobbled: boolean

  current: () => Track | null
  playQueue: (tracks: Track[], startIndex?: number) => void
  playNow: (track: Track) => void
  addNext: (track: Track) => void
  addToQueue: (tracks: Track[]) => void
  removeAt: (index: number) => void
  clearQueue: () => void
  toggle: () => void
  setPlaying: (playing: boolean) => void
  next: (auto?: boolean) => void
  previous: () => void
  seek: (seconds: number) => void
  setPosition: (seconds: number) => void
  setDuration: (seconds: number) => void
  setVolume: (volume: number) => void
  toggleMute: () => void
  toggleShuffle: () => void
  cycleRepeat: () => void
  markScrobbled: () => void
}

const VOLUME_KEY = 'musicdrome.volume'

/** Fisher-Yates, keeping the currently playing track at the head. */
function shuffleFrom(tracks: Track[], keepIndex: number): { queue: Track[]; index: number } {
  if (tracks.length < 2) return { queue: tracks, index: keepIndex }
  const head = tracks[keepIndex]
  const rest = tracks.filter((_, i) => i !== keepIndex)
  for (let i = rest.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1))
    ;[rest[i], rest[j]] = [rest[j], rest[i]]
  }
  return { queue: [head, ...rest], index: 0 }
}

export const usePlayer = create<PlayerState>((set, get) => ({
  queue: [],
  index: 0,
  playing: false,
  position: 0,
  duration: 0,
  volume: Number(localStorage.getItem(VOLUME_KEY) ?? 0.8),
  muted: false,
  shuffle: false,
  repeat: 'off',
  scrobbled: false,

  current() {
    const { queue, index } = get()
    return queue[index] ?? null
  },

  playQueue(tracks, startIndex = 0) {
    if (!tracks.length) return
    const state = get()
    if (state.shuffle) {
      const { queue, index } = shuffleFrom(tracks, startIndex)
      set({ queue, index, playing: true, position: 0, scrobbled: false })
    } else {
      set({ queue: tracks, index: startIndex, playing: true, position: 0, scrobbled: false })
    }
    void api.recordPlay(tracks[startIndex].id, false).catch(() => {})
  },

  playNow(track) {
    get().playQueue([track], 0)
  },

  addNext(track) {
    const { queue, index } = get()
    const copy = [...queue]
    copy.splice(index + 1, 0, track)
    set({ queue: copy })
  },

  addToQueue(tracks) {
    const { queue, playing } = get()
    const merged = [...queue, ...tracks]
    set({ queue: merged })
    if (!playing && !queue.length) set({ index: 0, playing: true, scrobbled: false })
  },

  removeAt(index) {
    const state = get()
    const copy = state.queue.filter((_, i) => i !== index)
    let nextIndex = state.index
    if (index < state.index) nextIndex -= 1
    else if (index === state.index) nextIndex = Math.min(state.index, copy.length - 1)
    set({
      queue: copy,
      index: Math.max(0, nextIndex),
      playing: copy.length ? state.playing : false,
    })
  },

  clearQueue() {
    set({ queue: [], index: 0, playing: false, position: 0, duration: 0 })
  },

  toggle() {
    if (!get().queue.length) return
    set({ playing: !get().playing })
  },

  setPlaying(playing) {
    set({ playing })
  },

  next(auto = false) {
    const { queue, index, repeat } = get()
    if (!queue.length) return

    if (auto && repeat === 'one') {
      set({ position: 0, scrobbled: false, playing: true })
      return
    }
    if (index < queue.length - 1) {
      set({ index: index + 1, position: 0, scrobbled: false, playing: true })
      void api.recordPlay(queue[index + 1].id, false).catch(() => {})
      return
    }
    if (repeat === 'all') {
      set({ index: 0, position: 0, scrobbled: false, playing: true })
      return
    }
    // End of queue: stop rather than silently looping
    set({ playing: false, position: 0 })
  },

  previous() {
    const { queue, index, position } = get()
    if (!queue.length) return
    // Mirror the usual convention: restart the track unless we're near its start
    if (position > 3) {
      set({ position: 0 })
      return
    }
    if (index > 0) set({ index: index - 1, position: 0, scrobbled: false, playing: true })
    else set({ position: 0 })
  },

  seek(seconds) {
    set({ position: seconds })
  },
  setPosition(seconds) {
    set({ position: seconds })
  },
  setDuration(seconds) {
    set({ duration: seconds })
  },

  setVolume(volume) {
    const clamped = Math.max(0, Math.min(1, volume))
    localStorage.setItem(VOLUME_KEY, String(clamped))
    set({ volume: clamped, muted: clamped === 0 })
  },

  toggleMute() {
    set({ muted: !get().muted })
  },

  toggleShuffle() {
    const state = get()
    const shuffle = !state.shuffle
    if (shuffle && state.queue.length > 1) {
      const { queue, index } = shuffleFrom(state.queue, state.index)
      set({ shuffle, queue, index })
    } else {
      set({ shuffle })
    }
  },

  cycleRepeat() {
    const order: RepeatMode[] = ['off', 'all', 'one']
    const next = order[(order.indexOf(get().repeat) + 1) % order.length]
    set({ repeat: next })
  },

  markScrobbled() {
    set({ scrobbled: true })
  },
}))
