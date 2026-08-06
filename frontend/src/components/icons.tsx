/** Inline icon set — no icon package, so the bundle stays small and offline-safe. */
type Props = { className?: string; filled?: boolean }

const base = 'h-5 w-5'
const stroke = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.75,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
}

export const Play = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.1-6.86a1 1 0 0 0 0-1.68L9.54 4.3A1 1 0 0 0 8 5.14Z" />
  </svg>
)

export const Pause = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <rect x="6" y="5" width="4" height="14" rx="1" />
    <rect x="14" y="5" width="4" height="14" rx="1" />
  </svg>
)

export const SkipNext = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M6 5.5v13a1 1 0 0 0 1.55.83L16 13.8V18a1 1 0 0 0 2 0V6a1 1 0 1 0-2 0v4.2L7.55 4.67A1 1 0 0 0 6 5.5Z" />
  </svg>
)

export const SkipPrev = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M18 5.5v13a1 1 0 0 1-1.55.83L8 13.8V18a1 1 0 0 1-2 0V6a1 1 0 0 1 2 0v4.2l8.45-5.53A1 1 0 0 1 18 5.5Z" />
  </svg>
)

export const Shuffle = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5" />
  </svg>
)

export const Repeat = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M17 2l4 4-4 4" />
    <path d="M3 11v-1a4 4 0 0 1 4-4h14M7 22l-4-4 4-4" />
    <path d="M21 13v1a4 4 0 0 1-4 4H3" />
  </svg>
)

export const RepeatOne = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M17 2l4 4-4 4" />
    <path d="M3 11v-1a4 4 0 0 1 4-4h14M7 22l-4-4 4-4" />
    <path d="M21 13v1a4 4 0 0 1-4 4H3" />
    <path d="M11 10h1v4" strokeWidth="2" />
  </svg>
)

export const Volume = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M11 5 6 9H3v6h3l5 4V5Z" fill="currentColor" stroke="none" />
    <path d="M16 9a4 4 0 0 1 0 6M19 6a8 8 0 0 1 0 12" />
  </svg>
)

export const VolumeOff = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M11 5 6 9H3v6h3l5 4V5Z" fill="currentColor" stroke="none" />
    <path d="m16 9 5 6M21 9l-5 6" />
  </svg>
)

export const Heart = ({ className = base, filled = false }: Props) => (
  <svg
    className={className}
    viewBox="0 0 24 24"
    fill={filled ? 'currentColor' : 'none'}
    stroke="currentColor"
    strokeWidth={1.75}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M12 20.5S3.5 15 3.5 9.2A4.7 4.7 0 0 1 12 6.4a4.7 4.7 0 0 1 8.5 2.8c0 5.8-8.5 11.3-8.5 11.3Z" />
  </svg>
)

export const Home = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="m3 10 9-7 9 7v9a2 2 0 0 1-2 2h-4v-6H9v6H5a2 2 0 0 1-2-2Z" />
  </svg>
)

export const Disc = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <circle cx="12" cy="12" r="9" />
    <circle cx="12" cy="12" r="2.5" />
  </svg>
)

export const Mic = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <rect x="9" y="2.5" width="6" height="11" rx="3" />
    <path d="M5 11a7 7 0 0 0 14 0M12 18v3.5" />
  </svg>
)

export const Note = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M9 18V5l11-2v13" />
    <circle cx="6" cy="18" r="3" />
    <circle cx="17" cy="16" r="3" />
  </svg>
)

export const List = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M4 6h16M4 12h16M4 18h10" />
  </svg>
)

export const Search = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5" />
  </svg>
)

export const Sparkles = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M12 3.5 13.8 9l5.5 1.8-5.5 1.8L12 18l-1.8-5.4L4.7 10.8 10.2 9Z" />
    <path d="M19 3v3M20.5 4.5h-3M6 17v2.5M7.2 18.2H4.8" />
  </svg>
)

export const Chart = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />
  </svg>
)

export const Radio = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <circle cx="12" cy="12" r="2.5" />
    <path d="M7.5 7.5a6.4 6.4 0 0 0 0 9M16.5 16.5a6.4 6.4 0 0 0 0-9M4.5 4.5a10.6 10.6 0 0 0 0 15M19.5 19.5a10.6 10.6 0 0 0 0-15" />
  </svg>
)

export const Compass = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <circle cx="12" cy="12" r="9" />
    <path d="m15.5 8.5-2 5-5 2 2-5Z" />
  </svg>
)

export const Cog = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2v.2a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 7 19.4a1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0-1.2-2.9H1a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 2.4 7a1.7 1.7 0 0 0-.3-1.9L2 5a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H7a1.7 1.7 0 0 0 1-1.5V1a2 2 0 1 1 4 0v.1A1.7 1.7 0 0 0 15 2.4a1.7 1.7 0 0 0 1.9-.3l.1-.1A2 2 0 1 1 19.8 5l-.1.1a1.7 1.7 0 0 0-.3 1.9V7c.2.6.8 1 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z" />
  </svg>
)

export const Shield = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M12 3l7 3v6c0 4.5-3 7.8-7 9-4-1.2-7-4.5-7-9V6Z" />
    <path d="m9.5 12 1.8 1.8 3.4-3.6" />
  </svg>
)

export const Download = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M12 3v11m0 0 4-4m-4 4-4-4M4 18v1.5A1.5 1.5 0 0 0 5.5 21h13a1.5 1.5 0 0 0 1.5-1.5V18" />
  </svg>
)

export const Upload = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M12 21V10m0 0 4 4m-4-4-4 4M4 6V4.5A1.5 1.5 0 0 1 5.5 3h13A1.5 1.5 0 0 1 20 4.5V6" />
  </svg>
)

export const Pencil = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17v3ZM14.5 7.5l2 2" />
  </svg>
)

export const FileMusic = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z" />
    <path d="M14 3v5h5M10 18v-4.5l4-1V17" />
    <circle cx="9" cy="18" r="1.2" />
    <circle cx="13" cy="17" r="1.2" />
  </svg>
)

export const Grip = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01" strokeWidth="2.5" />
  </svg>
)

export const Plus = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M12 5v14M5 12h14" />
  </svg>
)

export const Trash = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M4 7h16M10 4h4M6 7l1 13h10l1-13M10 11v6M14 11v6" />
  </svg>
)

export const Check = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="m5 13 4.5 4.5L19 7" />
  </svg>
)

export const X = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M6 6l12 12M18 6 6 18" />
  </svg>
)

export const Refresh = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5" />
  </svg>
)

export const Logout = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 16l-4-4 4-4M6 12h11" />
  </svg>
)

export const Star = ({ className = base, filled = false }: Props) => (
  <svg
    className={className}
    viewBox="0 0 24 24"
    fill={filled ? 'currentColor' : 'none'}
    stroke="currentColor"
    strokeWidth={1.5}
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="m12 3.6 2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.5 9.8l5.9-.9Z" />
  </svg>
)

export const Queue = ({ className = base }: Props) => (
  <svg className={className} viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M4 6h11M4 11h11M4 16h7" />
    <path d="M17 12v7" />
    <circle cx="15.5" cy="19" r="1.8" />
  </svg>
)
