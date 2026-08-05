import { useState } from 'react'
import { ArtistCard, Grid } from '../components/Cards'
import { EmptyState, ErrorBanner, Loading, Toggle } from '../components/ui'
import { api } from '../lib/api'
import { useAsync, useDebounced } from '../lib/hooks'

export default function Artists() {
  const [query, setQuery] = useState('')
  const [starredOnly, setStarredOnly] = useState(false)
  const debouncedQuery = useDebounced(query)

  const { data: artists, loading, error } = useAsync(
    () => api.artists({ q: debouncedQuery, starred: starredOnly, limit: 500 }),
    [debouncedQuery, starredOnly],
  )

  return (
    <div className="space-y-5" data-testid="artists-page">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="page-title">Artists</h1>
        <div className="flex items-center gap-4">
          <label className="flex items-center gap-2 text-sm text-muted">
            <Toggle checked={starredOnly} onChange={setStarredOnly} label="Favourites only" />
            Favourites only
          </label>
          <input
            className="input w-56"
            placeholder="Filter artists…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            data-testid="artist-filter"
          />
        </div>
      </header>

      <ErrorBanner message={error} />

      {loading ? (
        <Loading label="Loading artists" />
      ) : !artists?.length ? (
        <EmptyState
          title={starredOnly ? 'No favourite artists yet' : 'No artists found'}
          description={
            starredOnly
              ? 'Star an artist from their page and they will show up here.'
              : 'Scan your library from the Admin page to populate it.'
          }
        />
      ) : (
        <Grid>
          {artists.map((artist) => (
            <ArtistCard key={artist.id} artist={artist} />
          ))}
        </Grid>
      )}
    </div>
  )
}
