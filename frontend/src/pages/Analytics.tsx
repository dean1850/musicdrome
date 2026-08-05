import { useState } from 'react'
import { Chart, Refresh, Sparkles } from '../components/icons'
import { BarList, ColumnChart, EmptyState, ErrorBanner, Loading, Spinner, StatTile, Tabs } from '../components/ui'
import { api } from '../lib/api'
import { count, durationLong, relativeTime } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { useAuth } from '../store/auth'

type Period = 'week' | 'month' | 'quarter' | 'year' | 'all'

export default function Analytics() {
  const { user, server } = useAuth()
  const [period, setPeriod] = useState<Period>('month')

  const { data: stats, loading, error } = useAsync(() => api.analytics(period), [period])
  const aiAvailable = Boolean(server?.features?.ai) && Boolean(user?.ai_enabled)

  if (loading) return <Loading label="Crunching your listening history" />
  if (error) return <ErrorBanner message={error} />
  if (!stats) return null

  if (stats.totals.plays === 0) {
    return (
      <div className="space-y-5">
        <h1 className="page-title">Analytics</h1>
        <EmptyState
          icon={<Chart className="h-10 w-10" />}
          title="No listening data yet"
          description="Play some music and your statistics will build up here — top artists, listening clock, genre mix and an AI-written summary."
        />
      </div>
    )
  }

  const change = stats.comparison?.play_change_pct
  const hours = Object.entries(stats.by_hour).map(([hour, plays]) => ({
    label: Number(hour) % 3 === 0 ? hour : '',
    value: plays,
  }))
  const weekdays = Object.entries(stats.by_weekday).map(([day, plays]) => ({
    label: day.slice(0, 3),
    value: plays,
  }))

  return (
    <div className="space-y-8" data-testid="analytics-page">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="page-title">Analytics</h1>
          <p className="mt-1 text-sm text-muted">How you actually listened.</p>
        </div>
        <Tabs
          value={period}
          onChange={setPeriod}
          options={[
            { value: 'week', label: 'Week' },
            { value: 'month', label: 'Month' },
            { value: 'quarter', label: 'Quarter' },
            { value: 'year', label: 'Year' },
            { value: 'all', label: 'All time' },
          ]}
        />
      </header>

      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          label="Plays"
          value={count(stats.totals.plays)}
          hint={
            change === undefined
              ? undefined
              : `${change >= 0 ? '+' : ''}${change}% vs previous ${period}`
          }
        />
        <StatTile label="Listening time" value={durationLong(stats.totals.listening_seconds)} />
        <StatTile
          label="Artists"
          value={count(stats.totals.unique_artists)}
          hint={stats.new_artist_count ? `${stats.new_artist_count} new` : undefined}
        />
        <StatTile
          label="Longest streak"
          value={`${stats.listening_streak_days} day${stats.listening_streak_days === 1 ? '' : 's'}`}
          hint={`${Math.round(stats.repeat_ratio * 100)}% repeat listening`}
        />
      </section>

      {aiAvailable && <Insights period={period} />}

      <section className="grid gap-6 lg:grid-cols-2">
        <div className="card p-5">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-subtle">Top artists</h2>
          <BarList items={stats.top_artists.map((a) => ({ label: a.name, value: a.plays }))} />
        </div>
        <div className="card p-5">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-subtle">Top tracks</h2>
          <BarList items={stats.top_tracks.map((t) => ({ label: t.name, value: t.plays }))} />
        </div>
        <div className="card p-5">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-subtle">Genre mix</h2>
          <BarList
            items={stats.genre_mix.slice(0, 10).map((g) => ({ label: g.genre, value: g.plays }))}
            emptyLabel="No genre tags in your library yet"
          />
        </div>
        <div className="card p-5">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-subtle">Top albums</h2>
          <BarList items={stats.top_albums.map((a) => ({ label: a.name, value: a.plays }))} />
        </div>
      </section>

      <section className="grid gap-6 lg:grid-cols-2">
        <div className="card p-5">
          <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-subtle">
            Listening clock
          </h2>
          <ColumnChart data={hours} />
          <p className="mt-2 text-xs text-subtle">Plays by hour of day</p>
        </div>
        <div className="card p-5">
          <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-subtle">
            By day of week
          </h2>
          <ColumnChart data={weekdays} />
        </div>
      </section>

      {stats.new_artists.length > 0 && (
        <section className="card p-5">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-subtle">
            First heard this {period}
          </h2>
          <div className="flex flex-wrap gap-2">
            {stats.new_artists.map((name) => (
              <span key={name} className="chip">
                {name}
              </span>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}

function Insights({ period }: { period: string }) {
  const [refreshing, setRefreshing] = useState(false)
  const { data, loading, error, reload } = useAsync(() => api.insights(period), [period])

  async function refresh() {
    setRefreshing(true)
    try {
      await api.insights(period, true)
      await reload()
    } finally {
      setRefreshing(false)
    }
  }

  if (loading) {
    return (
      <section className="card p-5">
        <p className="text-sm text-muted">Generating your listening report…</p>
      </section>
    )
  }

  if (error || !data) {
    return (
      <section className="card p-5">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="flex items-center gap-2 text-sm font-semibold text-white">
              <Sparkles className="h-4 w-4 text-accent-soft" /> AI listening report
            </h2>
            <p className="mt-1 text-sm text-muted">
              {error || 'Not available yet.'}
            </p>
          </div>
          <button className="btn-outline shrink-0" onClick={refresh} disabled={refreshing}>
            {refreshing ? <Spinner className="h-4 w-4" /> : <Refresh className="h-4 w-4" />} Try again
          </button>
        </div>
      </section>
    )
  }

  const { payload } = data

  return (
    <section className="card overflow-hidden" data-testid="ai-insights">
      <div className="border-b border-line bg-gradient-to-r from-accent/10 to-transparent px-5 py-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="flex items-center gap-2 text-sm font-semibold text-white">
              <Sparkles className="h-4 w-4 text-accent-soft" /> AI listening report
            </h2>
            <p className="mt-1.5 text-lg font-medium text-white">{payload.headline}</p>
          </div>
          <button className="btn-ghost shrink-0" onClick={refresh} disabled={refreshing} aria-label="Regenerate">
            {refreshing ? <Spinner className="h-4 w-4" /> : <Refresh className="h-4 w-4" />}
          </button>
        </div>
      </div>

      <div className="space-y-5 px-5 py-5">
        <p className="whitespace-pre-line text-sm leading-relaxed text-muted">{payload.summary}</p>

        {payload.listening_personality && (
          <div className="rounded-lg border border-line bg-elevated px-4 py-3">
            <span className="text-xs uppercase tracking-wide text-subtle">Your listening style</span>
            <p className="mt-1 text-sm text-zinc-100">{payload.listening_personality}</p>
          </div>
        )}

        {payload.observations?.length > 0 && (
          <div className="grid gap-3 sm:grid-cols-2">
            {payload.observations.map((observation) => (
              <div key={observation.title} className="rounded-lg border border-line px-4 py-3">
                <h3 className="text-sm font-medium text-zinc-100">{observation.title}</h3>
                <p className="mt-1 text-sm text-muted">{observation.detail}</p>
              </div>
            ))}
          </div>
        )}

        {payload.suggestions?.length > 0 && (
          <div>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-subtle">
              Worth trying next
            </h3>
            <ul className="space-y-1.5">
              {payload.suggestions.map((suggestion) => (
                <li key={suggestion} className="flex gap-2 text-sm text-muted">
                  <span className="text-accent-soft">→</span>
                  {suggestion}
                </li>
              ))}
            </ul>
          </div>
        )}

        <p className="text-[11px] text-subtle">
          Written by {data.model} · {relativeTime(data.created_at)}
        </p>
      </div>
    </section>
  )
}
