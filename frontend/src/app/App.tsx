import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react'

import { MODE } from '../api/mode'
import { rest } from '../api/rest'
import { ErrorBoundary } from '../components/ErrorBoundary'
import { Kbd, Skeleton } from '../components/ui'
import { recordedLabel, unitTitle } from '../data/derive'
import { recorded } from '../data/recorded'
import { Overview } from '../screens/Overview'
import { Policy } from '../screens/Policy'
import { Results } from '../screens/Results'
import { Review } from '../screens/Review'
import { CommandPalette, type Command } from './CommandPalette'
import { navigate, ROUTES, useLocation, type RouteId } from './router'
import { useTheme } from './theme'
import { useAsync } from './useAsync'

const MOD_KEY = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.userAgent) ? '⌘' : 'Ctrl'

const Pipeline = lazy(() => import('../screens/Pipeline'))
const Repair = lazy(() => import('../screens/Repair'))

function isTyping(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName) || !!target.closest('.monaco-editor'))
  )
}

function BackendStatus() {
  const [health] = useAsync((signal) => rest.health(signal), [])
  const text =
    health.kind === 'loading' ? 'checking' : health.kind === 'error' ? 'unreachable' : `${health.data.status}`
  const tone = health.kind === 'ok' ? 'ok' : health.kind === 'error' ? 'fail' : 'skipped'
  return (
    <span className={`topbar__chip topbar__chip--${tone}`} role="status">
      API {text}
    </span>
  )
}

export function App() {
  const location = useLocation()
  const [theme, toggleTheme] = useTheme()
  const [paletteOpen, setPaletteOpen] = useState(false)
  const label = recordedLabel(recorded)

  const go = useCallback((route: RouteId, params?: Record<string, string>) => navigate(route, params), [])

  const commands = useMemo<Command[]>(() => {
    const screens = ROUTES.map((r) => ({
      id: `go-${r.id}`,
      label: r.label,
      group: 'Go to',
      hint: r.key,
      run: () => go(r.id),
    }))
    const repairUnits = recorded.units
      .filter((u) => u.milestone === 'v0.5')
      .map((u) => ({
        id: `repair-${u.key}`,
        label: unitTitle(recorded, u),
        group: 'Repair',
        hint: u.status,
        run: () => go('repair', { unit: u.key }),
      }))
    const pipelineUnits = recorded.units.map((u) => ({
      id: `pipeline-${u.key}`,
      label: unitTitle(recorded, u),
      group: 'Pipeline',
      hint: u.status,
      run: () => go('pipeline', { unit: u.key }),
    }))
    return [
      ...screens,
      { id: 'theme', label: `Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`, group: 'Settings', run: toggleTheme },
      ...repairUnits,
      ...pipelineUnits,
    ]
  }, [go, theme, toggleTheme])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen((open) => !open)
        return
      }
      if (paletteOpen || e.ctrlKey || e.metaKey || e.altKey || isTyping(e.target)) return
      const route = ROUTES.find((r) => r.key === e.key)
      if (route) {
        e.preventDefault()
        go(route.id)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [go, paletteOpen])

  useEffect(() => {
    const route = ROUTES.find((r) => r.id === location.route)
    document.title = route && route.id !== 'overview' ? `${route.label} · MORPH` : 'MORPH'
    document.getElementById('main')?.focus({ preventScroll: true })
    window.scrollTo({ top: 0 })
  }, [location.route])

  const screen = (() => {
    switch (location.route) {
      case 'pipeline':
        return <Pipeline params={location.params} />
      case 'repair':
        return <Repair params={location.params} />
      case 'results':
        return <Results />
      case 'review':
        return <Review />
      case 'policy':
        return <Policy />
      default:
        return <Overview />
    }
  })()

  return (
    <div className="shell">
      <a className="skip-link" href="#main" onClick={(e) => {
        e.preventDefault()
        document.getElementById('main')?.focus()
      }}>
        Skip to content
      </a>
      <aside className="sidebar">
        <div className="brand">
          <span className="brand__mark" aria-hidden="true" />
          <span className="brand__name">MORPH</span>
          <span className="brand__tag">integration engineer</span>
        </div>
        <nav aria-label="Screens">
          <ul className="nav">
            {ROUTES.map((r) => (
              <li key={r.id}>
                <a
                  className="nav__item"
                  href={`#/${r.id}`}
                  aria-current={location.route === r.id ? 'page' : undefined}
                >
                  <span className="nav__key" aria-hidden="true">
                    {r.key}
                  </span>
                  <span className="nav__label">{r.label}</span>
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <div className="sidebar__foot">
          <button type="button" className="button button--ghost" onClick={() => setPaletteOpen(true)}>
            Commands <Kbd>{MOD_KEY}</Kbd>
            <Kbd>K</Kbd>
          </button>
        </div>
      </aside>
      <div className="frame">
        <header className="topbar">
          <span className="topbar__recorded" title="Every figure on these screens is read from the committed results and replays">
            {label}
          </span>
          <span className="topbar__spacer" />
          {MODE === 'live' ? (
            <BackendStatus />
          ) : (
            <span className="topbar__chip" title="Static build: no backend, key, Docker or database">
              Demo · static data
            </span>
          )}
          <button
            type="button"
            className="button button--ghost topbar__theme"
            onClick={toggleTheme}
            aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
          >
            {theme === 'dark' ? 'Light' : 'Dark'}
          </button>
        </header>
        <main id="main" className="main" tabIndex={-1}>
          <ErrorBoundary key={location.route}>
            <Suspense fallback={<div className="page"><Skeleton lines={6} label="Loading screen" /></div>}>
              {screen}
            </Suspense>
          </ErrorBoundary>
        </main>
      </div>
      {paletteOpen && <CommandPalette commands={commands} onClose={() => setPaletteOpen(false)} />}
    </div>
  )
}
