import { useCallback, useEffect, useState, useSyncExternalStore } from 'react'

export type Theme = 'dark' | 'light'

const KEY = 'morph.theme'

/** Dark by default. The choice is a per-viewer convenience; storage may be unavailable. */
function stored(): Theme {
  try {
    return window.localStorage.getItem(KEY) === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(stored)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try {
      window.localStorage.setItem(KEY, theme)
    } catch {
      // Private windows and blocked storage: the theme still applies for this visit.
    }
  }, [theme])

  const toggle = useCallback(() => setTheme((t) => (t === 'dark' ? 'light' : 'dark')), [])
  return [theme, toggle]
}

function subscribeTheme(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange)
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  return () => observer.disconnect()
}

/** The applied theme, for components such as the graph and the editor that theme themselves. */
export function useAppliedTheme(): Theme {
  return useSyncExternalStore(subscribeTheme, () =>
    document.documentElement.dataset.theme === 'light' ? 'light' : 'dark',
  )
}

function subscribeMotion(onChange: () => void): () => void {
  const query = window.matchMedia('(prefers-reduced-motion: reduce)')
  query.addEventListener('change', onChange)
  return () => query.removeEventListener('change', onChange)
}

export function useReducedMotion(): boolean {
  return useSyncExternalStore(subscribeMotion, () => window.matchMedia('(prefers-reduced-motion: reduce)').matches)
}
