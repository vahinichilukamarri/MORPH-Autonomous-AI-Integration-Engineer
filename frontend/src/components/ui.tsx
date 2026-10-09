import type { ReactNode } from 'react'

import type { Tone } from '../data/derive'

export function Badge({ tone, children, title }: { tone: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge--${tone}`} title={title}>
      <span className="badge__dot" aria-hidden="true" />
      {children}
    </span>
  )
}

export function Chip({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span className="chip" title={title}>
      {children}
    </span>
  )
}

export function Panel({
  title,
  actions,
  children,
  className = '',
  labelledBy,
}: {
  title?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  labelledBy?: string
}) {
  return (
    <section className={`panel ${className}`} aria-labelledby={labelledBy}>
      {(title || actions) && (
        <header className="panel__head">
          {title && <h2 id={labelledBy} className="panel__title">{title}</h2>}
          {actions && <div className="panel__actions">{actions}</div>}
        </header>
      )}
      <div className="panel__body">{children}</div>
    </section>
  )
}

export function PageHead({ title, lede, aside }: { title: string; lede: ReactNode; aside?: ReactNode }) {
  return (
    <header className="page__head">
      <div>
        <h1>{title}</h1>
        <p className="lede">{lede}</p>
      </div>
      {aside && <div className="page__aside">{aside}</div>}
    </header>
  )
}

export function Metric({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: Tone }) {
  return (
    <div className={`metric${tone ? ` metric--${tone}` : ''}`}>
      <div className="metric__label">{label}</div>
      <div className="metric__value">{value}</div>
      {sub && <div className="metric__sub">{sub}</div>}
    </div>
  )
}

export function Skeleton({ lines = 3, label = 'Loading' }: { lines?: number; label?: string }) {
  return (
    <div className="skeleton" role="status" aria-live="polite">
      <span className="visually-hidden">{label}</span>
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="skeleton__line" style={{ width: `${92 - ((i * 17) % 40)}%` }} />
      ))}
    </div>
  )
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state state--empty">
      <div className="state__title">{title}</div>
      {children && <div className="state__body">{children}</div>}
    </div>
  )
}

export function ErrorState({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <div className="state state--error" role="alert">
      <div className="state__title">Could not load this</div>
      <div className="state__body mono">{error.message}</div>
      {onRetry && (
        <button type="button" className="button" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  )
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>
}

export function FixtureBanner({ children }: { children: ReactNode }) {
  return (
    <div className="fixture-banner" role="note">
      <strong>FIXTURE</strong>
      <span>{children}</span>
    </div>
  )
}

export interface SegmentOption<T extends string> {
  value: T
  label: ReactNode
  tone?: Tone
}

/** A radio group drawn as a segmented control; arrow keys move the selection. */
export function Segmented<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string
  options: SegmentOption<T>[]
  value: T
  onChange: (value: T) => void
}) {
  const move = (delta: number) => {
    const i = options.findIndex((o) => o.value === value)
    const next = options[(i + delta + options.length) % options.length]
    onChange(next.value)
    requestAnimationFrame(() => {
      document.querySelector<HTMLButtonElement>(`[data-seg="${label}"][data-value="${next.value}"]`)?.focus()
    })
  }
  return (
    <div className="seg" role="radiogroup" aria-label={label}>
      {options.map((o) => {
        const checked = o.value === value
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={checked}
            tabIndex={checked ? 0 : -1}
            data-seg={label}
            data-value={o.value}
            className={`seg__item${o.tone ? ` seg__item--${o.tone}` : ''}`}
            onClick={() => onChange(o.value)}
            onKeyDown={(e) => {
              if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
                e.preventDefault()
                move(1)
              } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
                e.preventDefault()
                move(-1)
              }
            }}
          >
            {o.label}
          </button>
        )
      })}
    </div>
  )
}
