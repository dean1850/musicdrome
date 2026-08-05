import { useState } from 'react'
import { Download, Play, Plus, Radio, Refresh, Trash, X } from '../components/icons'
import { EmptyState, ErrorBanner, Loading, Modal, Spinner, Toast, Toggle } from '../components/ui'
import { api } from '../lib/api'
import { bytes, date, duration as fmtDuration } from '../lib/format'
import { useAsync } from '../lib/hooks'
import type { PodcastEpisode } from '../types'

export default function Podcasts() {
  const { data: channels, loading, error, reload } = useAsync(() => api.podcasts(), [])
  const [selected, setSelected] = useState<number | null>(null)
  const [addOpen, setAddOpen] = useState(false)
  const [toast, setToast] = useState('')

  if (loading) return <Loading label="Loading podcasts" />

  return (
    <div className="space-y-5" data-testid="podcasts-page">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="page-title">Podcasts</h1>
          <p className="mt-1 text-sm text-muted">
            Subscriptions sync to Subsonic clients too.
          </p>
        </div>
        <div className="flex gap-2">
          <button
            className="btn-outline"
            onClick={async () => {
              await api.refreshAllPodcasts()
              setToast('Refreshing all feeds in the background')
            }}
          >
            <Refresh className="h-4 w-4" /> Refresh all
          </button>
          <button className="btn-primary" onClick={() => setAddOpen(true)} data-testid="add-podcast">
            <Plus className="h-4 w-4" /> Subscribe
          </button>
        </div>
      </header>

      <ErrorBanner message={error} />

      {!channels?.length ? (
        <EmptyState
          icon={<Radio className="h-10 w-10" />}
          title="No subscriptions yet"
          description="Paste an RSS feed URL to subscribe. Episodes can stream on demand or download automatically."
          action={
            <button className="btn-primary" onClick={() => setAddOpen(true)}>
              <Plus className="h-4 w-4" /> Subscribe to a feed
            </button>
          }
        />
      ) : (
        <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {channels.map((channel) => (
            <li key={channel.id} className="card flex gap-3 p-4" data-testid="podcast-channel">
              <img
                src={channel.image_url || api.coverUrl('podcast', channel.id, 160)}
                alt=""
                className="h-16 w-16 shrink-0 rounded-lg border border-line object-cover"
                onError={(e) => {
                  e.currentTarget.src = api.coverUrl('podcast', channel.id, 160)
                }}
              />
              <div className="min-w-0 flex-1">
                <button
                  onClick={() => setSelected(channel.id)}
                  className="block w-full truncate text-left font-medium text-zinc-100 hover:text-white"
                >
                  {channel.title}
                </button>
                <p className="truncate text-xs text-muted">{channel.author || channel.url}</p>
                <p className="mt-1 text-xs text-subtle">
                  {channel.episode_count} episodes
                  {channel.status === 'error' && <span className="text-red-300"> · feed error</span>}
                </p>
                <div className="mt-2 flex items-center gap-2">
                  <button
                    onClick={() => setSelected(channel.id)}
                    className="btn-ghost px-2 py-1 text-xs"
                    data-testid="open-podcast"
                  >
                    Episodes
                  </button>
                  <button
                    onClick={async () => {
                      await api.unsubscribePodcast(channel.id)
                      setToast(`Unsubscribed from ${channel.title}`)
                      void reload()
                    }}
                    className="btn-ghost px-2 py-1 text-xs text-subtle hover:text-red-400"
                    aria-label="Unsubscribe"
                  >
                    <Trash className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}

      <AddPodcastModal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        onDone={(title) => {
          setToast(`Subscribed to ${title}`)
          void reload()
        }}
      />

      {selected !== null && (
        <ChannelModal channelId={selected} onClose={() => setSelected(null)} onToast={setToast} />
      )}

      <Toast message={toast} onDone={() => setToast('')} />
    </div>
  )
}

function AddPodcastModal({
  open,
  onClose,
  onDone,
}: {
  open: boolean
  onClose: () => void
  onDone: (title: string) => void
}) {
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit() {
    setBusy(true)
    setError('')
    try {
      const channel = await api.subscribePodcast(url.trim())
      onDone(channel.title || url)
      setUrl('')
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not subscribe to that feed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title="Subscribe to a podcast"
      onClose={onClose}
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn-primary" onClick={submit} disabled={busy || !url.trim()} data-testid="submit-podcast">
            {busy && <Spinner className="h-4 w-4" />} Subscribe
          </button>
        </>
      }
    >
      <ErrorBanner message={error} onDismiss={() => setError('')} />
      <label className="label" htmlFor="feed-url">
        RSS feed URL
      </label>
      <input
        id="feed-url"
        className="input"
        placeholder="https://example.com/feed.xml"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        autoFocus
        data-testid="podcast-url"
      />
      <p className="mt-2 text-xs text-subtle">
        Musicdrome reads the feed immediately and lists its episodes. Nothing is downloaded until
        you ask for it, unless auto-download is on for that channel.
      </p>
    </Modal>
  )
}

function ChannelModal({
  channelId,
  onClose,
  onToast,
}: {
  channelId: number
  onClose: () => void
  onToast: (message: string) => void
}) {
  const { data, loading, reload } = useAsync(() => api.podcast(channelId), [channelId])
  const [playing, setPlaying] = useState<PodcastEpisode | null>(null)

  return (
    <Modal open title={data?.channel.title || 'Podcast'} onClose={onClose} wide>
      {loading || !data ? (
        <Loading label="Loading episodes" />
      ) : (
        <div className="space-y-4">
          <div className="flex items-start gap-4">
            <img
              src={data.channel.image_url || api.coverUrl('podcast', channelId, 200)}
              alt=""
              className="h-20 w-20 shrink-0 rounded-lg border border-line object-cover"
            />
            <div className="min-w-0 flex-1">
              <p className="text-sm text-muted line-clamp-3">{data.channel.description}</p>
              <label className="mt-3 flex items-center gap-2 text-sm text-muted">
                <Toggle
                  checked={data.channel.auto_download}
                  onChange={async (value) => {
                    await api.updatePodcast(channelId, value)
                    void reload()
                  }}
                  label="Auto-download new episodes"
                />
                Auto-download new episodes
              </label>
            </div>
            <button
              className="btn-outline shrink-0"
              onClick={async () => {
                await api.refreshPodcast(channelId)
                onToast('Feed refreshed')
                void reload()
              }}
            >
              <Refresh className="h-4 w-4" /> Refresh
            </button>
          </div>

          {playing && (
            <div className="rounded-lg border border-line bg-elevated p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <p className="truncate text-sm text-zinc-100">{playing.title}</p>
                <button onClick={() => setPlaying(null)} className="text-subtle hover:text-white" aria-label="Close player">
                  <X className="h-4 w-4" />
                </button>
              </div>
              {/* Episodes play in their own element so the music queue isn't disturbed */}
              <audio
                controls
                autoPlay
                className="w-full"
                src={api.episodeStreamUrl(playing.id)}
                data-testid="episode-audio"
              />
            </div>
          )}

          <ul className="space-y-2">
            {data.episodes.map((episode) => (
              <li
                key={episode.id}
                className="flex items-start gap-3 rounded-lg border border-line px-3 py-2.5"
                data-testid="podcast-episode"
              >
                <div className="min-w-0 flex-1">
                  <h4 className="truncate text-sm font-medium text-zinc-100">{episode.title}</h4>
                  <p className="text-xs text-subtle">
                    {date(episode.publish_date)}
                    {episode.duration ? ` · ${fmtDuration(episode.duration)}` : ''}
                    {episode.size ? ` · ${bytes(episode.size)}` : ''}
                    {` · ${episode.status}`}
                  </p>
                  {episode.error_message && (
                    <p className="mt-0.5 text-xs text-red-300">{episode.error_message}</p>
                  )}
                </div>

                {episode.status === 'completed' ? (
                  <button
                    className="btn-ghost shrink-0 px-2 py-1 text-xs"
                    onClick={() => setPlaying(episode)}
                    data-testid="play-episode"
                  >
                    <Play className="h-3.5 w-3.5" /> Play
                  </button>
                ) : (
                  <button
                    className="btn-ghost shrink-0 px-2 py-1 text-xs"
                    onClick={async () => {
                      await api.downloadEpisode(episode.id)
                      onToast('Download started')
                      setTimeout(() => void reload(), 2500)
                    }}
                    data-testid="download-episode"
                  >
                    <Download className="h-3.5 w-3.5" /> Download
                  </button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Modal>
  )
}
