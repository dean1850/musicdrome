import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { AlbumCard, Grid } from '../components/Cards'
import TrackList from '../components/TrackList'
import { Heart, Play, Shuffle } from '../components/icons'
import { ErrorBanner, Loading, Tabs } from '../components/ui'
import { api } from '../lib/api'
import { count } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { usePlayer } from '../store/player'

export default function ArtistDetail() {
  const { id } = useParams()
  const artistId = Number(id)
  const { playQueue, toggleShuffle, shuffle } = usePlayer()

  const [tab, setTab] = useState<'albums' | 'tracks' | 'about'>('albums')
  const [starred, setStarred] = useState<boolean | null>(null)

  const { data, loading, error } = useAsync(() => api.artist(artistId), [artistId])
  const { data: tracks } = useAsync(() => api.artistTracks(artistId), [artistId])

  if (loading) return <Loading label="Loading artist" />
  if (error) return <ErrorBanner message={error} />
  if (!data) return null

  const { artist, albums, similar } = data
  const isStarred = starred ?? artist.starred

  async function toggleStar() {
    const value = !isStarred
    setStarred(value)
    try {
      await api.star('artist', artistId, value)
    } catch {
      setStarred(!value)
    }
  }

  function playAll(shuffled = false) {
    if (!tracks?.length) return
    if (shuffled && !shuffle) toggleShuffle()
    playQueue(tracks, shuffled ? Math.floor(Math.random() * tracks.length) : 0)
  }

  return (
    <div className="space-y-8" data-testid="artist-detail">
      <header className="flex flex-col gap-6 sm:flex-row sm:items-end">
        <img
          src={api.coverUrl('artist', artist.id, 500)}
          alt=""
          className="h-44 w-44 shrink-0 rounded-full border border-line object-cover shadow-2xl"
        />
        <div className="min-w-0 flex-1">
          <p className="text-xs uppercase tracking-wide text-subtle">Artist</p>
          <h1 className="mt-1 text-3xl font-bold tracking-tight text-white" data-testid="artist-name">
            {artist.name}
          </h1>
          <p className="mt-2 text-sm text-muted">
            {count(artist.album_count)} albums · {count(artist.track_count)} tracks
          </p>
          <div className="mt-5 flex flex-wrap items-center gap-2">
            <button className="btn-primary" onClick={() => playAll(false)} disabled={!tracks?.length}>
              <Play className="h-4 w-4" /> Play
            </button>
            <button className="btn-outline" onClick={() => playAll(true)} disabled={!tracks?.length}>
              <Shuffle className="h-4 w-4" /> Shuffle
            </button>
            <button
              className={`btn-outline ${isStarred ? 'text-accent-soft' : ''}`}
              onClick={toggleStar}
              data-testid="star-artist"
            >
              <Heart className="h-4 w-4" filled={isStarred} />
              {isStarred ? 'Favourited' : 'Favourite'}
            </button>
          </div>
        </div>
      </header>

      <Tabs
        value={tab}
        onChange={setTab}
        options={[
          { value: 'albums', label: `Albums (${albums.length})` },
          { value: 'tracks', label: `Tracks (${tracks?.length ?? 0})` },
          { value: 'about', label: 'About' },
        ]}
      />

      {tab === 'albums' &&
        (albums.length ? (
          <Grid>
            {albums.map((album) => (
              <AlbumCard key={album.id} album={album} />
            ))}
          </Grid>
        ) : (
          <p className="text-sm text-muted">No albums indexed for this artist.</p>
        ))}

      {tab === 'tracks' && <TrackList tracks={tracks || []} hideArtist />}

      {tab === 'about' && (
        <div className="grid gap-8 lg:grid-cols-3">
          <div className="lg:col-span-2">
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-subtle">Biography</h2>
            {artist.biography ? (
              <div
                className="prose-sm max-w-none space-y-3 text-sm leading-relaxed text-muted [&_a]:text-accent-soft"
                dangerouslySetInnerHTML={{ __html: sanitize(artist.biography) }}
              />
            ) : (
              <p className="text-sm text-subtle">
                No biography yet. Configure a Last.fm API key in .env and Musicdrome will fetch one
                during the next enrichment pass.
              </p>
            )}
            {artist.mbid && (
              <p className="mt-4 text-xs text-subtle">
                MusicBrainz ID: <code className="text-zinc-400">{artist.mbid}</code>
              </p>
            )}
          </div>

          <div>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-subtle">Similar artists</h2>
            {similar.length ? (
              <ul className="space-y-1.5">
                {similar.map((entry) => (
                  <li key={entry.name} className="flex items-center justify-between gap-2 text-sm">
                    <span className="truncate text-zinc-200">{entry.name}</span>
                    {entry.in_library ? (
                      <Link to={`/search?q=${encodeURIComponent(entry.name)}`} className="chip shrink-0 text-accent-soft">
                        in library
                      </Link>
                    ) : (
                      <span className="chip shrink-0">{Math.round(entry.score * 100)}%</span>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-subtle">
                None cached yet — similarity data arrives with Last.fm or ListenBrainz enrichment.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * Last.fm biographies come back as HTML with a link back to the site. Strip
 * everything except the anchors we are willing to render.
 */
function sanitize(html: string): string {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/<style[\s\S]*?<\/style>/gi, '')
    .replace(/ on\w+="[^"]*"/gi, '')
    .replace(/<(?!\/?(a|p|br|em|strong|i|b)\b)[^>]*>/gi, '')
}
