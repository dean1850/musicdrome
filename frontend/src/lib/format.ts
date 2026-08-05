export function duration(seconds: number): string {
  if (!seconds || seconds < 0) return '0:00'
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const secs = Math.floor(seconds % 60)
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`
  return `${minutes}:${String(secs).padStart(2, '0')}`
}

/** Long-form duration for summaries: "3 days, 4 hours". */
export function durationLong(seconds: number): string {
  if (!seconds) return '0 minutes'
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const parts: string[] = []
  if (days) parts.push(`${days} day${days === 1 ? '' : 's'}`)
  if (hours) parts.push(`${hours} hour${hours === 1 ? '' : 's'}`)
  if (!days && minutes) parts.push(`${minutes} minute${minutes === 1 ? '' : 's'}`)
  return parts.join(', ') || 'under a minute'
}

export function bytes(value: number): string {
  if (!value) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1)
  return `${(value / Math.pow(1024, index)).toFixed(index === 0 ? 0 : 1)} ${units[index]}`
}

export function count(value: number): string {
  return new Intl.NumberFormat().format(value)
}

export function date(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '—'
  return parsed.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

export function relativeTime(value: string | null | undefined): string {
  if (!value) return 'never'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return 'never'

  const seconds = Math.round((Date.now() - parsed.getTime()) / 1000)
  const table: [number, Intl.RelativeTimeFormatUnit][] = [
    [60, 'second'],
    [3600, 'minute'],
    [86400, 'hour'],
    [604800, 'day'],
    [2629800, 'week'],
    [31557600, 'month'],
  ]
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })

  if (seconds < 60) return formatter.format(-seconds, 'second')
  for (let i = 1; i < table.length; i++) {
    const [limit, unit] = table[i]
    if (seconds < limit) {
      const divisor = table[i - 1][0]
      return formatter.format(-Math.round(seconds / divisor), unit)
    }
  }
  return formatter.format(-Math.round(seconds / 31557600), 'year')
}
