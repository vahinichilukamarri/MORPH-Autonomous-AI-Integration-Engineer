import { useSyncExternalStore } from 'react'

export type Theme = 'dark' | 'light'

const KEY = 'morph.theme'

/** Dark by default. index.html applies a saved choice before first paint; storage may be unavailable. */
function apply(theme: Theme): void {
  document.documentElement.dataset.theme = theme
}

export function getTheme(): Theme {
  return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'
}

export function setTheme(theme: Theme): void {
  apply(theme)
  try {
    window.localStorage.setItem(KEY, theme)
  } catch {
    // Private windows and blocked storage: the theme still applies for this visit.
  }
}

export function toggleTheme(): void {
  setTheme(getTheme() === 'dark' ? 'light' : 'dark')
}

function subscribeTheme(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange)
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  return () => observer.disconnect()
}

/** The applied theme; every component that reads it re-renders when it changes. */
export function useTheme(): Theme {
  return useSyncExternalStore(subscribeTheme, getTheme)
}

function subscribeMotion(onChange: () => void): () => void {
  const query = window.matchMedia('(prefers-reduced-motion: reduce)')
  query.addEventListener('change', onChange)
  return () => query.removeEventListener('change', onChange)
}

export function useReducedMotion(): boolean {
  return useSyncExternalStore(subscribeMotion, () => window.matchMedia('(prefers-reduced-motion: reduce)').matches)
}
