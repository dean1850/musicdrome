import { useSearchParams } from 'react-router-dom'
import { AlbumCard, ArtistCard, Grid } from '../components/Cards'
import TrackList from '../components/TrackList'
import { Search as SearchIcon } from '../components/icons'
import { EmptyState, ErrorBanner, Loading } from '../components/ui'
import { api } from '../lib/api'
import { useAsync } from '../lib/hooks'

export default function Search() {
  const [params] = useSearchParams()
  const query = params.get('q') || ''

  const { data, loading, error } = useAsync(
    () => (query ? api.search(query, 30) : Promise.resolve(null)),
    [query],
  )

  if (!query) {
    return (
      <EmptyState
        icon={<SearchIcon className="h-10 w-10" />}
        title="Search your library"
        description="Find artists, albums and tracks. Press / anywhere to jump to the search box."
      />
    )
  }

  if (loading) return <Loading label={`Searching for “${query}”`} />
  if (error) return <ErrorBanner message={error} />
  if (!data) return null

  const empty = !data.artists.length && !data.albums.length && !data.tracks.length

  return (
    <div className="space-y-8" data-testid="search-page">
      <header>
        <h1 className="page-title">
          Results for <span className="text-accent-soft">“{query}”</span>
        </h1>
        <p className="mt-1 text-sm text-muted">
          {data.artists.length} artists · {data.albums.length} albums · {data.tracks.length} tracks
        </p>
      </header>

      {empty && (
        <EmptyState
          title="Nothing matched"
          description="Try a shorter or differently spelled term. Search covers titles, artists and album names."
        />
      )}

      {data.artists.length > 0 && (
        <section>
          <h2 className="mb-2 text-lg font-semibold text-white">Artists</h2>
          <Grid>
            {data.artists.map((artist) => (
              <ArtistCard key={artist.id} artist={artist} />
            ))}
          </Grid>
        </section>
      )}

      {data.albums.length > 0 && (
        <section>
          <h2 className="mb-2 text-lg font-semibold text-white">Albums</h2>
          <Grid>
            {data.albums.map((album) => (
              <AlbumCard key={album.id} album={album} />
            ))}
          </Grid>
        </section>
      )}

      {data.tracks.length > 0 && (
        <section>
          <h2 className="mb-3 text-lg font-semibold text-white">Tracks</h2>
          <TrackList tracks={data.tracks} />
        </section>
      )}
    </div>
  )
}
