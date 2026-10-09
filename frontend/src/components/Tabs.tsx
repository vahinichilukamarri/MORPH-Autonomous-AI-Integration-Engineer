import * as m from 'motion/react-m'
import { useId, useRef, type ReactNode } from 'react'

export interface TabItem<T extends string> {
  id: T
  label: ReactNode
}

/** WAI-ARIA tabs with roving focus (arrows, Home, End). The active indicator glides between tabs
 * through a shared layout id; with reduced motion it simply moves. */
export function Tabs<T extends string>({
  label,
  tabs,
  value,
  onChange,
  panel,
}: {
  label: string
  tabs: TabItem<T>[]
  value: T
  onChange: (id: T) => void
  panel: ReactNode
}) {
  const base = useId()
  const list = useRef<HTMLDivElement>(null)
  const focusTab = (id: T) => list.current?.querySelector<HTMLButtonElement>(`[data-tab="${id}"]`)?.focus()
  const move = (index: number) => {
    const next = tabs[(index + tabs.length) % tabs.length]
    onChange(next.id)
    focusTab(next.id)
  }
  return (
    <div className="tabs">
      <div className="tabs__list" role="tablist" aria-label={label} ref={list}>
        {tabs.map((t, i) => {
          const selected = t.id === value
          return (
            <button
              key={t.id}
              type="button"
              role="tab"
              id={`${base}-tab-${t.id}`}
              aria-selected={selected}
              aria-controls={`${base}-panel`}
              tabIndex={selected ? 0 : -1}
              data-tab={t.id}
              className="tabs__tab"
              onClick={() => onChange(t.id)}
              onKeyDown={(e) => {
                if (e.key === 'ArrowRight') move(i + 1)
                else if (e.key === 'ArrowLeft') move(i - 1)
                else if (e.key === 'Home') move(0)
                else if (e.key === 'End') move(tabs.length - 1)
                else return
                e.preventDefault()
              }}
            >
              {t.label}
              {selected && <m.span className="tabs__indicator" layoutId={`${base}-indicator`} aria-hidden="true" />}
            </button>
          )
        })}
      </div>
      <div className="tabs__panel" role="tabpanel" id={`${base}-panel`} aria-labelledby={`${base}-tab-${value}`} tabIndex={0}>
        {panel}
      </div>
    </div>
  )
}
