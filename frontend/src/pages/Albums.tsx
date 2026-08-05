import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { AlbumCard, Grid } from '../components/Cards'
import { EmptyState, ErrorBanner, Loading } from '../components/ui'
import { api } from '../lib/api'
import { useAsync, useDebounced } from '../lib/hooks'

const SORTS = [
  { value: 'name', label: 'A–Z' },
  { value: 'newest', label: 'Recently added' },
  { value: 'year', label: 'Year' },
  { value: 'artist', label: 'Artist' },
  { value: 'frequent', label: 'Most played' },
  { value: 'recent', label: 'Recently played' },
  { value: 'starred', label: 'Favourites' },
  { value: 'random', label: 'Random' },
]

const PAGE_SIZE = 60

export default function Albums() {
  const [params, setParams] = useSearchParams()
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(0)

  const sort = params.get('sort') || 'name'
  const genre = params.get('genre') || ''
  const debouncedQuery = useDebounced(query)

  const { data: genres } = useAsync(() => api.genres(), [])
  const { data: albums, loading, error } = useAsync(
    () => api.albums({ q: debouncedQuery, sort, genre, limit: PAGE_SIZE, offset: page * PAGE_SIZE }),
    [debouncedQuery, sort, genre, page],
  )

  function update(key: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    setParams(next)
    setPage(0)
  }

  return (
    <div className="space-y-5" data-testid="albums-page">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="page-title">Albums</h1>
        <div className="flex flex-wrap items-center gap-2">
          <input
            className="input w-48"
            placeholder="Filter albums…"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value)
              setPage(0)
            }}
            data-testid="album-filter"
          />
          <select
            className="input w-40"
            value={genre}
            onChange={(e) => update('genre', e.target.value)}
            aria-label="Genre"
          >
            <option value="">All genres</option>
            {(genres || []).map((g) => (
              <option key={g.name} value={g.name}>
                {g.name}
              </option>
            ))}
          </select>
          <select
            className="input w-44"
            value={sort}
            onChange={(e) => update('sort', e.target.value)}
            aria-label="Sort"
            data-testid="album-sort"
          >
            {SORTS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      </header>

      <ErrorBanner message={error} />

      {loading ? (
        <Loading label="Loading albums" />
      ) : !albums?.length ? (
        <EmptyState
          title={query || genre ? 'No albums match those filters' : 'No albums yet'}
          description={
            query || genre
              ? 'Try a different search term or clear the genre filter.'
              : 'Scan your library from the Admin page to populate it.'
          }
        />
      ) : (
        <>
          <Grid>
            {albums.map((album) => (
              <AlbumCard key={album.id} album={album} />
            ))}
          </Grid>

          <div className="flex items-center justify-center gap-3 pt-2">
            <button
              className="btn-outline"
              disabled={page === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              Previous
            </button>
            <span className="text-sm text-subtle">Page {page + 1}</span>
            <button
              className="btn-outline"
              disabled={albums.length < PAGE_SIZE}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
            </button>
          </div>
        </>
      )}
    </div>
  )
}
