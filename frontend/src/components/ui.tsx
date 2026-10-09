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

/** A titled surface. `labelledBy` names the section for assistive technology. */
export function Card({
  title,
  actions,
  children,
  className = '',
  labelledBy,
  flush = false,
}: {
  title?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  labelledBy?: string
  flush?: boolean
}) {
  return (
    <section className={`card ${className}`} aria-labelledby={title ? labelledBy : undefined}>
      {(title || actions) && (
        <header className="card__head">
          {title && (
            <h2 id={labelledBy} className="card__title">
              {title}
            </h2>
          )}
          {actions && <div className="card__actions">{actions}</div>}
        </header>
      )}
      <div className={flush ? 'card__body card__body--flush' : 'card__body'}>{children}</div>
    </section>
  )
}

export function PageHead({
  eyebrow,
  title,
  lede,
  aside,
}: {
  eyebrow?: ReactNode
  title: string
  lede: ReactNode
  aside?: ReactNode
}) {
  return (
    <header className="page__head">
      <div className="page__intro">
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h1>{title}</h1>
        <p className="lede">{lede}</p>
      </div>
      {aside && <div className="page__aside">{aside}</div>}
    </header>
  )
}

export function Metric({ label, value, sub, tone }: { label: ReactNode; value: ReactNode; sub?: ReactNode; tone?: Tone }) {
  return (
    <div className={`metric${tone ? ` metric--${tone}` : ''}`}>
      <div className="metric__label">{label}</div>
      <div className="metric__value">{value}</div>
      {sub && <div className="metric__sub">{sub}</div>}
    </div>
  )
}

/** Shimmering placeholder lines; the shimmer is CSS and stops under reduced motion. */
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

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="state state--empty">
      <div className="state__title">{title}</div>
      {children && <div className="state__body">{children}</div>}
      {action}
    </div>
  )
}

export function ErrorState({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <div className="state state--error" role="alert">
      <div className="state__title">Could not load this</div>
      <div className="state__body mono">{error.message}</div>
      {onRetry && (
        <button type="button" className="btn btn--secondary" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  )
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>
}

/** The on-screen label for anything that is not built yet. In code these are FIXTURE data. */
export function PreviewTag() {
  return <span className="preview-tag">Preview</span>
}

export function PreviewBanner({ children }: { children: ReactNode }) {
  return (
    <div className="preview-banner" role="note">
      <PreviewTag />
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

/** Renders the small markdown subset used in README cells: `code`, **bold** and [links](path). Links
 * to repository paths go to the file on GitHub. */
export function InlineMarkdown({ text, repoUrl }: { text: string; repoUrl: string }) {
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\))/g).filter(Boolean)
  return (
    <>
      {parts.map((p, i) => {
        if (p.startsWith('`')) return <code key={i}>{p.slice(1, -1)}</code>
        if (p.startsWith('**')) return <strong key={i}>{p.slice(2, -2)}</strong>
        const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(p)
        if (link) {
          return (
            <a key={i} href={repoLink(repoUrl, link[2])} target="_blank" rel="noreferrer">
              {link[1]}
            </a>
          )
        }
        return <span key={i}>{p}</span>
      })}
    </>
  )
}

export function repoLink(repoUrl: string, path: string): string {
  if (/^https?:/.test(path)) return path
  return `${repoUrl}/${path.endsWith('/') ? 'tree' : 'blob'}/main/${path}`
}
