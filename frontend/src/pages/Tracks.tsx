import { useState } from 'react'
import TrackList from '../components/TrackList'
import { Play, Shuffle } from '../components/icons'
import { ErrorBanner, Loading } from '../components/ui'
import { api } from '../lib/api'
import { useAsync, useDebounced } from '../lib/hooks'
import { usePlayer } from '../store/player'

const SORTS = [
  { value: 'title', label: 'Title' },
  { value: 'artist', label: 'Artist' },
  { value: 'album', label: 'Album' },
  { value: 'newest', label: 'Recently added' },
  { value: 'frequent', label: 'Most played' },
  { value: 'random', label: 'Random' },
]

export default function Tracks() {
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState('title')
  const [genre, setGenre] = useState('')
  const debouncedQuery = useDebounced(query)
  const { playQueue, toggleShuffle, shuffle } = usePlayer()

  const { data: genres } = useAsync(() => api.genres(), [])
  const { data: tracks, loading, error } = useAsync(
    () => api.tracks({ q: debouncedQuery, sort, genre, limit: 300 }),
    [debouncedQuery, sort, genre],
  )

  return (
    <div className="space-y-5" data-testid="tracks-page">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="page-title">Tracks</h1>
        <div className="flex flex-wrap items-center gap-2">
          <button
            className="btn-outline"
            disabled={!tracks?.length}
            onClick={() => {
              if (!tracks?.length) return
              if (!shuffle) toggleShuffle()
              playQueue(tracks, Math.floor(Math.random() * tracks.length))
            }}
          >
            <Shuffle className="h-4 w-4" /> Shuffle all
          </button>
          <button
            className="btn-primary"
            disabled={!tracks?.length}
            onClick={() => tracks && playQueue(tracks, 0)}
          >
            <Play className="h-4 w-4" /> Play all
          </button>
          <input
            className="input w-48"
            placeholder="Filter tracks…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            data-testid="track-filter"
          />
          <select className="input w-40" value={genre} onChange={(e) => setGenre(e.target.value)} aria-label="Genre">
            <option value="">All genres</option>
            {(genres || []).map((g) => (
              <option key={g.name} value={g.name}>
                {g.name}
              </option>
            ))}
          </select>
          <select className="input w-40" value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort">
            {SORTS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      </header>

      <ErrorBanner message={error} />
      {loading ? <Loading label="Loading tracks" /> : <TrackList tracks={tracks || []} />}
    </div>
  )
}
