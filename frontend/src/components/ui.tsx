import { useEffect, type ReactNode } from 'react'
import { Star, X } from './icons'

export function Spinner({ className = 'h-5 w-5' }: { className?: string }) {
  return (
    <svg className={`animate-spin ${className}`} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity="0.2" strokeWidth="3" />
      <path
        d="M22 12a10 10 0 0 0-10-10"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinecap="round"
      />
    </svg>
  )
}

export function Loading({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-3 py-16 text-muted" data-testid="loading">
      <Spinner />
      <span className="text-sm">{label}…</span>
    </div>
  )
}

export function EmptyState({
  title,
  description,
  action,
  icon,
}: {
  title: string
  description?: string
  action?: ReactNode
  icon?: ReactNode
}) {
  return (
    <div
      className="flex flex-col items-center justify-center rounded-xl border border-dashed border-line px-6 py-16 text-center"
      data-testid="empty-state"
    >
      {icon && <div className="mb-4 text-subtle">{icon}</div>}
      <h3 className="text-base font-medium text-zinc-200">{title}</h3>
      {description && <p className="mt-1.5 max-w-md text-sm text-muted">{description}</p>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  )
}

export function ErrorBanner({ message, onDismiss }: { message: string; onDismiss?: () => void }) {
  if (!message) return null
  return (
    <div
      className="mb-4 flex items-start gap-3 rounded-lg border border-red-900/60 bg-red-950/40 px-4 py-3 text-sm text-red-200"
      role="alert"
      data-testid="error-banner"
    >
      <span className="flex-1">{message}</span>
      {onDismiss && (
        <button onClick={onDismiss} className="text-red-300 hover:text-red-100" aria-label="Dismiss">
          <X className="h-4 w-4" />
        </button>
      )}
    </div>
  )
}

export function Toast({ message, onDone }: { message: string; onDone: () => void }) {
  useEffect(() => {
    if (!message) return
    const timer = setTimeout(onDone, 4000)
    return () => clearTimeout(timer)
  }, [message, onDone])

  if (!message) return null
  return (
    <div
      className="fixed bottom-28 left-1/2 z-50 -translate-x-1/2 animate-fade-in rounded-lg border border-line bg-elevated px-4 py-2.5 text-sm text-zinc-100 shadow-xl"
      role="status"
      data-testid="toast"
    >
      {message}
    </div>
  )
}

export function Modal({
  open,
  title,
  onClose,
  children,
  footer,
  wide = false,
}: {
  open: boolean
  title: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  wide?: boolean
}) {
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" data-testid="modal">
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={onClose} />
      <div
        className={`relative z-10 w-full ${wide ? 'max-w-3xl' : 'max-w-lg'} animate-fade-in rounded-xl border border-line bg-surface shadow-2xl`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <header className="flex items-center justify-between border-b border-line px-5 py-3.5">
          <h2 className="text-sm font-semibold text-white">{title}</h2>
          <button onClick={onClose} className="text-muted hover:text-white" aria-label="Close">
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="max-h-[65vh] overflow-y-auto px-5 py-4">{children}</div>
        {footer && (
          <footer className="flex justify-end gap-2 border-t border-line px-5 py-3.5">{footer}</footer>
        )}
      </div>
    </div>
  )
}

export function Stars({
  value,
  onChange,
  size = 'h-4 w-4',
}: {
  value: number
  onChange?: (rating: number) => void
  size?: string
}) {
  return (
    <div className="flex items-center gap-0.5" data-testid="stars">
      {[1, 2, 3, 4, 5].map((n) => (
        <button
          key={n}
          type="button"
          disabled={!onChange}
          // Clicking the current rating clears it, which is what people expect
          onClick={() => onChange?.(value === n ? 0 : n)}
          className={`${onChange ? 'hover:text-amber-300' : 'cursor-default'} ${
            n <= value ? 'text-amber-400' : 'text-zinc-600'
          }`}
          aria-label={`${n} star${n === 1 ? '' : 's'}`}
        >
          <Star className={size} filled={n <= value} />
        </button>
      ))}
    </div>
  )
}

export function StatTile({
  label,
  value,
  hint,
}: {
  label: string
  value: string | number
  hint?: string
}) {
  return (
    <div className="card px-4 py-3.5" data-testid={`stat-${label.toLowerCase().replace(/\s+/g, '-')}`}>
      <div className="text-xs uppercase tracking-wide text-subtle">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums text-white">{value}</div>
      {hint && <div className="mt-0.5 text-xs text-muted">{hint}</div>}
    </div>
  )
}

/** Horizontal bar list — used for top artists/genres, avoids a chart library. */
export function BarList({
  items,
  emptyLabel = 'No data yet',
}: {
  items: { label: string; value: number }[]
  emptyLabel?: string
}) {
  if (!items.length) return <p className="py-4 text-sm text-muted">{emptyLabel}</p>
  const max = Math.max(...items.map((i) => i.value), 1)

  return (
    <ul className="space-y-2">
      {items.map((item) => (
        <li key={item.label}>
          <div className="mb-1 flex items-baseline justify-between gap-3 text-sm">
            <span className="truncate text-zinc-200">{item.label}</span>
            <span className="tabular-nums text-subtle">{item.value}</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-elevated">
            <div
              className="h-full rounded-full bg-accent"
              style={{ width: `${Math.max(2, (item.value / max) * 100)}%` }}
            />
          </div>
        </li>
      ))}
    </ul>
  )
}

/** Vertical column chart for the listening clock. */
export function ColumnChart({ data }: { data: { label: string; value: number }[] }) {
  const max = Math.max(...data.map((d) => d.value), 1)
  return (
    <div className="flex h-32 items-end gap-1">
      {data.map((d) => (
        <div key={d.label} className="group flex flex-1 flex-col items-center gap-1">
          <div
            className="w-full rounded-t bg-accent/70 transition-colors group-hover:bg-accent"
            style={{ height: `${Math.max(2, (d.value / max) * 100)}%` }}
            title={`${d.label}: ${d.value}`}
          />
          <span className="text-[10px] text-subtle">{d.label}</span>
        </div>
      ))}
    </div>
  )
}

export function Tabs<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T
  onChange: (value: T) => void
  options: { value: T; label: string }[]
}) {
  return (
    <div className="inline-flex rounded-lg border border-line bg-surface p-1" role="tablist">
      {options.map((option) => (
        <button
          key={option.value}
          role="tab"
          aria-selected={value === option.value}
          onClick={() => onChange(option.value)}
          data-testid={`tab-${option.value}`}
          className={`rounded-md px-3 py-1.5 text-sm transition-colors ${
            value === option.value
              ? 'bg-accent text-white'
              : 'text-muted hover:bg-elevated hover:text-zinc-100'
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

export function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean
  onChange: (value: boolean) => void
  label?: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${
        checked ? 'bg-accent' : 'bg-zinc-700'
      }`}
    >
      <span
        className={`absolute top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
          checked ? 'translate-x-5' : 'translate-x-0.5'
        }`}
      />
    </button>
  )
}
