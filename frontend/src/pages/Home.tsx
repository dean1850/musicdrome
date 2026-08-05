import { Link } from 'react-router-dom'
import { AlbumCard, Grid } from '../components/Cards'
import { Disc, Sparkles } from '../components/icons'
import { EmptyState, Loading, StatTile } from '../components/ui'
import { api } from '../lib/api'
import { count, durationLong } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { useAuth } from '../store/auth'

export default function Home() {
  const { user } = useAuth()

  const { data, loading } = useAsync(
    async () => {
      const [stats, recent, added, random, playlists] = await Promise.all([
        api.stats(),
        api.albums({ sort: 'recent', limit: 12 }).catch(() => []),
        api.albums({ sort: 'newest', limit: 12 }),
        api.albums({ sort: 'random', limit: 12 }),
        api.playlists('all').catch(() => []),
      ])
      return { stats, recent, added, random, playlists }
    },
    [],
  )

  if (loading) return <Loading label="Loading your library" />
  if (!data) return null

  const { stats, recent, added, random, playlists } = data
  const hour = new Date().getHours()
  const greeting = hour < 5 ? 'Still up' : hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening'

  if (stats.tracks === 0) {
    return (
      <div className="space-y-6">
        <h1 className="page-title">{greeting}, {user?.username}</h1>
        <EmptyState
          icon={<Disc className="h-10 w-10" />}
          title="Your library is empty"
          description="Point MUSIC_DIR at your music folder in .env, then run a scan. Musicdrome will index tags, cover art and albums automatically."
          action={
            user?.is_admin ? (
              <Link to="/admin" className="btn-primary">
                Go to Admin → Scan library
              </Link>
            ) : (
              <span className="text-sm text-subtle">Ask an administrator to scan the library.</span>
            )
          }
        />
      </div>
    )
  }

  return (
    <div className="space-y-8" data-testid="home-page">
      <header>
        <h1 className="page-title">
          {greeting}, {user?.username}
        </h1>
        <p className="mt-1 text-sm text-muted">Here's what's in your library right now.</p>
      </header>

      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile label="Tracks" value={count(stats.tracks)} />
        <StatTile label="Albums" value={count(stats.albums)} />
        <StatTile label="Artists" value={count(stats.artists)} />
        <StatTile label="Total time" value={durationLong(stats.duration)} hint={`${count(stats.plays)} plays by you`} />
      </section>

      {playlists.length > 0 && (
        <section>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-lg font-semibold text-white">Your playlists</h2>
            <Link to="/playlists" className="text-sm text-muted hover:text-white">
              See all
            </Link>
          </div>
          <div className="flex flex-wrap gap-2">
            {playlists.slice(0, 8).map((playlist) => (
              <Link
                key={playlist.id}
                to={`/playlists/${playlist.id}`}
                className="flex items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2 text-sm text-zinc-200 transition-colors hover:border-zinc-600 hover:bg-elevated"
              >
                {playlist.is_ai && <Sparkles className="h-3.5 w-3.5 text-accent-soft" />}
                <span className="max-w-[14rem] truncate">{playlist.name}</span>
                <span className="text-xs text-subtle">{playlist.song_count}</span>
              </Link>
            ))}
          </div>
        </section>
      )}

      {recent.length > 0 && (
        <Section title="Recently played" to="/albums?sort=recent" albums={recent} />
      )}
      <Section title="Recently added" to="/albums?sort=newest" albums={added} />
      <Section title="From the shelves" to="/albums?sort=random" albums={random} />
    </div>
  )
}

function Section({
  title,
  to,
  albums,
}: {
  title: string
  to: string
  albums: import('../types').Album[]
}) {
  if (!albums.length) return null
  return (
    <section>
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-lg font-semibold text-white">{title}</h2>
        <Link to={to} className="text-sm text-muted hover:text-white">
          See all
        </Link>
      </div>
      <Grid>
        {albums.map((album) => (
          <AlbumCard key={album.id} album={album} />
        ))}
      </Grid>
    </section>
  )
}
