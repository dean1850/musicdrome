import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { duration as fmtDuration } from '../lib/format'
import { usePlayer } from '../store/player'
import {
  Heart,
  Pause,
  Play,
  Queue,
  Repeat,
  RepeatOne,
  Shuffle,
  SkipNext,
  SkipPrev,
  Trash,
  Volume,
  VolumeOff,
  X,
} from './icons'

export default function Player() {
  const audioRef = useRef<HTMLAudioElement>(null)
  const [queueOpen, setQueueOpen] = useState(false)
  const [starred, setStarred] = useState(false)
  const [seeking, setSeeking] = useState<number | null>(null)

  const {
    queue,
    index,
    playing,
    position,
    duration,
    volume,
    muted,
    shuffle,
    repeat,
    scrobbled,
    toggle,
    next,
    previous,
    setPosition,
    setDuration,
    setPlaying,
    setVolume,
    toggleMute,
    toggleShuffle,
    cycleRepeat,
    markScrobbled,
    removeAt,
    clearQueue,
  } = usePlayer()

  const track = queue[index] ?? null

  // Load a new source whenever the track changes
  useEffect(() => {
    const audio = audioRef.current
    if (!audio || !track) return
    audio.src = api.streamUrl(track.id)
    audio.load()
    setStarred(track.starred)
    if (playing) void audio.play().catch(() => setPlaying(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [track?.id])

  useEffect(() => {
    const audio = audioRef.current
    if (!audio || !track) return
    if (playing) void audio.play().catch(() => setPlaying(false))
    else audio.pause()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, track?.id])

  useEffect(() => {
    const audio = audioRef.current
    if (audio) audio.volume = muted ? 0 : volume
  }, [volume, muted])

  // Scrobble once a play is "real": half the track, or four minutes in.
  useEffect(() => {
    if (!track || scrobbled) return
    const threshold = Math.min(track.duration / 2, 240)
    if (threshold > 0 && position >= threshold) {
      markScrobbled()
      void api.recordPlay(track.id, true).catch(() => {})
    }
  }, [position, track, scrobbled, markScrobbled])

  // Space toggles playback, unless the user is typing
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return
      if (event.code === 'Space') {
        event.preventDefault()
        toggle()
      } else if (event.code === 'ArrowRight' && event.shiftKey) next()
      else if (event.code === 'ArrowLeft' && event.shiftKey) previous()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [toggle, next, previous])

  async function toggleStar() {
    if (!track) return
    const value = !starred
    setStarred(value)
    try {
      await api.star('track', track.id, value)
    } catch {
      setStarred(!value)
    }
  }

  const shown = seeking ?? position
  const progress = duration > 0 ? (shown / duration) * 100 : 0

  return (
    <>
      <audio
        ref={audioRef}
        preload="metadata"
        data-testid="audio-element"
        onTimeUpdate={(e) => setPosition(e.currentTarget.currentTime)}
        onLoadedMetadata={(e) => setDuration(e.currentTarget.duration || track?.duration || 0)}
        onEnded={() => next(true)}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onError={() => setPlaying(false)}
      />

      <footer
        className="fixed inset-x-0 bottom-0 z-40 border-t border-line bg-surface/95 backdrop-blur"
        data-testid="player"
      >
        {/* Progress bar sits on the top edge, full bleed */}
        <div className="relative h-1 w-full bg-elevated">
          <div className="h-full bg-accent" style={{ width: `${progress}%` }} />
          <input
            type="range"
            min={0}
            max={duration || track?.duration || 0}
            step={0.5}
            value={shown}
            disabled={!track}
            onChange={(e) => setSeeking(Number(e.target.value))}
            onMouseUp={() => {
              if (seeking !== null && audioRef.current) audioRef.current.currentTime = seeking
              setSeeking(null)
            }}
            onTouchEnd={() => {
              if (seeking !== null && audioRef.current) audioRef.current.currentTime = seeking
              setSeeking(null)
            }}
            className="absolute inset-0 h-1 w-full cursor-pointer opacity-0"
            aria-label="Seek"
            data-testid="seek-bar"
          />
        </div>

        <div className="mx-auto flex h-20 max-w-screen-2xl items-center gap-4 px-4">
          {/* Now playing */}
          <div className="flex min-w-0 flex-1 items-center gap-3">
            {track ? (
              <>
                <img
                  src={api.coverUrl('track', track.id, 96)}
                  alt=""
                  className="h-12 w-12 shrink-0 rounded-md border border-line object-cover"
                />
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium text-white" data-testid="now-playing-title">
                    {track.title}
                  </div>
                  <div className="truncate text-xs text-muted">
                    {track.artist_id ? (
                      <Link to={`/artists/${track.artist_id}`} className="hover:text-zinc-200">
                        {track.artist_name}
                      </Link>
                    ) : (
                      track.artist_name
                    )}
                  </div>
                </div>
                <button
                  onClick={toggleStar}
                  className={`ml-1 shrink-0 ${starred ? 'text-accent-soft' : 'text-subtle hover:text-zinc-200'}`}
                  aria-label={starred ? 'Remove from favourites' : 'Add to favourites'}
                  data-testid="player-star"
                >
                  <Heart className="h-4 w-4" filled={starred} />
                </button>
              </>
            ) : (
              <div className="text-sm text-subtle" data-testid="player-idle">
                Nothing playing
              </div>
            )}
          </div>

          {/* Transport */}
          <div className="flex shrink-0 flex-col items-center gap-1">
            <div className="flex items-center gap-2">
              <button
                onClick={toggleShuffle}
                className={shuffle ? 'text-accent-soft' : 'text-subtle hover:text-zinc-200'}
                aria-label="Shuffle"
                aria-pressed={shuffle}
                data-testid="shuffle"
              >
                <Shuffle className="h-4 w-4" />
              </button>
              <button
                onClick={previous}
                disabled={!track}
                className="text-zinc-300 hover:text-white disabled:opacity-40"
                aria-label="Previous"
                data-testid="prev"
              >
                <SkipPrev />
              </button>
              <button
                onClick={toggle}
                disabled={!track}
                className="flex h-10 w-10 items-center justify-center rounded-full bg-white text-black transition hover:scale-105 disabled:opacity-40 disabled:hover:scale-100"
                aria-label={playing ? 'Pause' : 'Play'}
                data-testid="play-pause"
              >
                {playing ? <Pause className="h-5 w-5" /> : <Play className="ml-0.5 h-5 w-5" />}
              </button>
              <button
                onClick={() => next()}
                disabled={!track}
                className="text-zinc-300 hover:text-white disabled:opacity-40"
                aria-label="Next"
                data-testid="next"
              >
                <SkipNext />
              </button>
              <button
                onClick={cycleRepeat}
                className={repeat !== 'off' ? 'text-accent-soft' : 'text-subtle hover:text-zinc-200'}
                aria-label={`Repeat: ${repeat}`}
                data-testid="repeat"
              >
                {repeat === 'one' ? <RepeatOne className="h-4 w-4" /> : <Repeat className="h-4 w-4" />}
              </button>
            </div>
            <div className="flex items-center gap-2 text-[11px] tabular-nums text-subtle">
              <span data-testid="position">{fmtDuration(shown)}</span>
              <span>/</span>
              <span>{fmtDuration(duration || track?.duration || 0)}</span>
            </div>
          </div>

          {/* Volume + queue */}
          <div className="flex flex-1 items-center justify-end gap-3">
            <button
              onClick={() => setQueueOpen((open) => !open)}
              className={`${queueOpen ? 'text-accent-soft' : 'text-subtle hover:text-zinc-200'} relative`}
              aria-label="Queue"
              data-testid="queue-toggle"
            >
              <Queue className="h-5 w-5" />
              {queue.length > 0 && (
                <span className="absolute -right-1.5 -top-1.5 rounded-full bg-accent px-1 text-[10px] font-medium text-white">
                  {queue.length}
                </span>
              )}
            </button>
            <div className="hidden items-center gap-2 sm:flex">
              <button
                onClick={toggleMute}
                className="text-subtle hover:text-zinc-200"
                aria-label={muted ? 'Unmute' : 'Mute'}
              >
                {muted || volume === 0 ? <VolumeOff className="h-4 w-4" /> : <Volume className="h-4 w-4" />}
              </button>
              <input
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={muted ? 0 : volume}
                onChange={(e) => setVolume(Number(e.target.value))}
                className="w-24"
                aria-label="Volume"
                data-testid="volume"
              />
            </div>
          </div>
        </div>
      </footer>

      {/* Queue drawer */}
      {queueOpen && (
        <aside
          className="fixed bottom-[84px] right-4 z-40 flex max-h-[60vh] w-80 flex-col rounded-xl border border-line bg-surface shadow-2xl"
          data-testid="queue-panel"
        >
          <header className="flex items-center justify-between border-b border-line px-4 py-3">
            <h3 className="text-sm font-semibold text-white">Queue · {queue.length}</h3>
            <div className="flex items-center gap-2">
              <button
                onClick={clearQueue}
                className="text-subtle hover:text-red-400"
                aria-label="Clear queue"
              >
                <Trash className="h-4 w-4" />
              </button>
              <button
                onClick={() => setQueueOpen(false)}
                className="text-subtle hover:text-white"
                aria-label="Close queue"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </header>
          <ul className="flex-1 overflow-y-auto p-2">
            {queue.length === 0 && <li className="px-2 py-6 text-center text-sm text-subtle">Queue is empty</li>}
            {queue.map((item, i) => (
              <li
                key={`${item.id}-${i}`}
                className={`group flex items-center gap-2 rounded-lg px-2 py-2 text-sm ${
                  i === index ? 'bg-elevated text-white' : 'text-zinc-300 hover:bg-elevated'
                }`}
              >
                <button
                  onClick={() => usePlayer.setState({ index: i, playing: true, scrobbled: false })}
                  className="min-w-0 flex-1 text-left"
                >
                  <div className="truncate">{item.title}</div>
                  <div className="truncate text-xs text-subtle">{item.artist_name}</div>
                </button>
                <span className="shrink-0 text-xs tabular-nums text-subtle">
                  {fmtDuration(item.duration)}
                </span>
                <button
                  onClick={() => removeAt(i)}
                  className="shrink-0 text-subtle opacity-0 transition-opacity group-hover:opacity-100 hover:text-red-400"
                  aria-label={`Remove ${item.title} from queue`}
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </li>
            ))}
          </ul>
        </aside>
      )}
    </>
  )
}
