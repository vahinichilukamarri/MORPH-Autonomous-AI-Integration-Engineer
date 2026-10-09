import '../styles/app.css'

import * as m from 'motion/react-m'
import { lazy, Suspense, useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'

import { MODE } from '../api/mode'
import { rest } from '../api/rest'
import { Dialog } from '../components/Dialog'
import { ErrorBoundary } from '../components/ErrorBoundary'
import { Icon } from '../components/Icon'
import { Kbd, Skeleton } from '../components/ui'
import { Brand } from '../components/Brand'
import { recordedLabel, RUN_NAME, runUnits, unitTitle } from '../data/derive'
import { recorded } from '../data/recorded'
import type { Milestone } from '../data/types'
import { DURATION, EASE } from '../motion/tokens'
import { CommandPalette, type Command } from './CommandPalette'
import { href, LANDING_HREF, navigate, SCREENS, type ScreenId } from './router'
import { setRun, useRun } from './runs'
import { toggleTheme, useTheme } from './theme'
import { useAsync } from './useAsync'

const MOD = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.userAgent) ? '⌘' : 'Ctrl'

const Overview = lazy(() => import('../screens/Overview'))
const Pipeline = lazy(() => import('../screens/Pipeline'))
const Repair = lazy(() => import('../screens/Repair'))
const Results = lazy(() => import('../screens/Results'))
const Review = lazy(() => import('../screens/Review'))
const Policy = lazy(() => import('../screens/Policy'))

const RUNS: Milestone[] = ['v0.5', 'v0.4']

export const SHORTCUTS: { keys: string[]; action: string }[] = [
  { keys: [MOD, 'K'], action: 'Open the command palette' },
  ...SCREENS.map((s) => ({ keys: [s.key], action: `Go to ${s.label}` })),
  { keys: ['T'], action: 'Switch between dark and light theme' },
  { keys: ['['], action: 'Collapse or expand the sidebar' },
  { keys: ['?'], action: 'Show these shortcuts' },
  { keys: ['Esc'], action: 'Close a dialog or drawer' },
]

function isTyping(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName) || !!target.closest('.monaco-editor'))
  )
}

function storedCollapsed(): boolean {
  try {
    return window.localStorage.getItem('morph.sidebar') === 'collapsed'
  } catch {
    return false
  }
}

function BackendStatus() {
  const [health] = useAsync((signal) => rest.health(signal), [])
  const text = health.kind === 'loading' ? 'checking' : health.kind === 'error' ? 'unreachable' : `${health.data.status}`
  const tone = health.kind === 'ok' ? 'ok' : health.kind === 'error' ? 'fail' : 'skipped'
  return (
    <span className={`pill pill--${tone}`} role="status">
      API {text}
    </span>
  )
}

function Screen({ screen, params }: { screen: ScreenId; params: URLSearchParams }): ReactNode {
  switch (screen) {
    case 'pipeline':
      return <Pipeline params={params} />
    case 'repair':
      return <Repair params={params} />
    case 'results':
      return <Results />
    case 'review':
      return <Review />
    case 'policy':
      return <Policy />
    default:
      return <Overview />
  }
}

export default function AppShell({ screen, params }: { screen: ScreenId; params: URLSearchParams }) {
  const theme = useTheme()
  const run = useRun()
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(storedCollapsed)
  const info = SCREENS.find((s) => s.id === screen) ?? SCREENS[0]
  const unitKey = params.get('unit')
  const unit = unitKey ? recorded.units.find((u) => u.key === unitKey) : undefined

  const toggleSidebar = useCallback(() => {
    setCollapsed((c) => {
      try {
        window.localStorage.setItem('morph.sidebar', c ? 'expanded' : 'collapsed')
      } catch {
        // Storage blocked: the choice holds for this visit.
      }
      return !c
    })
  }, [])

  const commands = useMemo<Command[]>(() => {
    const screens = SCREENS.map((s) => ({ id: `go-${s.id}`, label: s.label, group: 'Go to', hint: s.key, run: () => navigate(s.id) }))
    const runs = RUNS.map((r) => ({
      id: `run-${r}`,
      label: `Scope to ${RUN_NAME[r]}`,
      group: 'Run',
      hint: r === run ? 'current' : undefined,
      run: () => setRun(r),
    }))
    const repair = runUnits(recorded, 'v0.5').map((u) => ({
      id: `repair-${u.key}`,
      label: unitTitle(recorded, u),
      group: 'Repair',
      hint: u.status,
      run: () => {
        setRun('v0.5')
        navigate('repair', { unit: u.key })
      },
    }))
    const pipeline = recorded.units.map((u) => ({
      id: `pipeline-${u.key}`,
      label: unitTitle(recorded, u),
      group: 'Pipeline',
      hint: u.status,
      run: () => {
        setRun(u.milestone)
        navigate('pipeline', { unit: u.key })
      },
    }))
    return [
      ...screens,
      ...runs,
      { id: 'theme', label: `Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`, group: 'Settings', hint: 'T', run: toggleTheme },
      { id: 'sidebar', label: collapsed ? 'Expand the sidebar' : 'Collapse the sidebar', group: 'Settings', hint: '[', run: toggleSidebar },
      { id: 'help', label: 'Keyboard shortcuts', group: 'Help', hint: '?', run: () => setHelpOpen(true) },
      { id: 'landing', label: 'Back to the MORPH site', group: 'Help', run: () => (window.location.hash = LANDING_HREF) },
      ...repair,
      ...pipeline,
    ]
  }, [theme, run, collapsed, toggleSidebar])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setHelpOpen(false)
        setPaletteOpen((open) => !open)
        return
      }
      if (paletteOpen || helpOpen || e.ctrlKey || e.metaKey || e.altKey || isTyping(e.target)) return
      const target = SCREENS.find((s) => s.key === e.key)
      if (target) {
        e.preventDefault()
        navigate(target.id)
      } else if (e.key === '?') {
        e.preventDefault()
        setHelpOpen(true)
      } else if (e.key === 't' || e.key === 'T') {
        e.preventDefault()
        toggleTheme()
      } else if (e.key === '[') {
        e.preventDefault()
        toggleSidebar()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [paletteOpen, helpOpen, toggleSidebar])

  useEffect(() => {
    document.title = `${info.label} · MORPH`
    document.getElementById('main')?.focus({ preventScroll: true })
    window.scrollTo({ top: 0 })
  }, [info.label])

  return (
    <div className={`shell${collapsed ? ' shell--collapsed' : ''}`}>
      <a
        className="skip-link"
        href="#main"
        onClick={(e) => {
          e.preventDefault()
          document.getElementById('main')?.focus()
        }}
      >
        Skip to content
      </a>
      <aside className="sidebar" aria-label="Sidebar">
        <div className="sidebar__brand">
          <a href={LANDING_HREF} className="sidebar__home" aria-label="MORPH home">
            <Brand compact={collapsed} />
          </a>
        </div>
        <nav aria-label="Screens" className="sidebar__nav">
          <ul className="nav">
            {SCREENS.map((s) => {
              const current = s.id === screen
              return (
                <li key={s.id}>
                  <a
                    className="nav__item"
                    href={href(s.id)}
                    aria-current={current ? 'page' : undefined}
                    aria-label={s.label}
                    title={collapsed ? s.label : undefined}
                  >
                    {current && <m.span className="nav__active" layoutId="nav-active" aria-hidden="true" transition={{ duration: DURATION.base, ease: EASE.out }} />}
                    <Icon name={s.icon} />
                    <span className="nav__label">{s.label}</span>
                    <span className="nav__key" aria-hidden="true">
                      {s.key}
                    </span>
                  </a>
                </li>
              )
            })}
          </ul>
        </nav>
        <div className="sidebar__foot">
          <button
            type="button"
            className="nav__item nav__item--button"
            onClick={() => setPaletteOpen(true)}
            aria-label="Open the command palette"
            title={collapsed ? 'Command palette' : undefined}
          >
            <Icon name="command" />
            <span className="nav__label">Commands</span>
            <span className="nav__key" aria-hidden="true">
              {MOD} K
            </span>
          </button>
          <button
            type="button"
            className="nav__item nav__item--button"
            onClick={toggleSidebar}
            aria-label={collapsed ? 'Expand the sidebar' : 'Collapse the sidebar'}
            aria-expanded={!collapsed}
          >
            <Icon name={collapsed ? 'chevronRight' : 'chevronLeft'} />
            <span className="nav__label">Collapse</span>
            <span className="nav__key" aria-hidden="true">
              [
            </span>
          </button>
        </div>
      </aside>

      <div className="frame">
        <header className="topbar">
          <nav aria-label="Breadcrumb" className="crumbs">
            <ol>
              <li>
                <a href={LANDING_HREF}>MORPH</a>
              </li>
              <li>
                <a href={href(screen)} aria-current={unit ? undefined : 'page'}>
                  {info.label}
                </a>
              </li>
              {unit && (
                <li>
                  <span aria-current="page" className="mono">
                    {unitTitle(recorded, unit)}
                  </span>
                </li>
              )}
            </ol>
          </nav>
          <span className="topbar__spacer" />
          <label className="run-select">
            <span className="visually-hidden">Recorded run</span>
            <select
              className="input input--sm"
              value={run}
              onChange={(e) => {
                const next = e.target.value as Milestone
                setRun(next)
                // A unit from the other run no longer matches the scope; fall back to that run's default.
                if (unit && unit.milestone !== next) navigate(screen)
              }}
            >
              {RUNS.map((r) => (
                <option key={r} value={r}>
                  {RUN_NAME[r]}
                </option>
              ))}
            </select>
          </label>
          {MODE === 'live' && <BackendStatus />}
          <button type="button" className="btn btn--icon btn--ghost" onClick={() => setPaletteOpen(true)} aria-label="Open the command palette">
            <Icon name="search" />
          </button>
          <button type="button" className="btn btn--icon btn--ghost" onClick={() => setHelpOpen(true)} aria-label="Keyboard shortcuts">
            <Icon name="keyboard" />
          </button>
          <button
            type="button"
            className="btn btn--icon btn--ghost"
            onClick={toggleTheme}
            aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
          >
            <Icon name={theme === 'dark' ? 'sun' : 'moon'} />
          </button>
        </header>
        <div className="runbar" role="note">
          <span className="runbar__dot" aria-hidden="true" />
          <span className="runbar__label" title="Every figure in the app is read from the committed results and replays">
            {recordedLabel(recorded, run)}
          </span>
          <span className="runbar__mode">{MODE === 'live' ? 'Live API where endpoints exist' : 'Demo · static recorded data, no backend'}</span>
        </div>
        <main id="main" className="main" tabIndex={-1}>
          <ErrorBoundary key={screen}>
            <Suspense
              fallback={
                <div className="page">
                  <Skeleton lines={8} label="Loading screen" />
                </div>
              }
            >
              <m.div key={screen} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: DURATION.base, ease: EASE.out }}>
                <Screen screen={screen} params={params} />
              </m.div>
            </Suspense>
          </ErrorBoundary>
        </main>
      </div>

      <CommandPalette open={paletteOpen} commands={commands} onClose={() => setPaletteOpen(false)} />
      <Dialog open={helpOpen} onClose={() => setHelpOpen(false)} title="Keyboard shortcuts" className="help">
        <dl className="shortcuts">
          {SHORTCUTS.map((s) => (
            <div key={s.action}>
              <dt>
                {s.keys.map((k) => (
                  <Kbd key={k}>{k}</Kbd>
                ))}
              </dt>
              <dd>{s.action}</dd>
            </div>
          ))}
        </dl>
      </Dialog>
    </div>
  )
}
