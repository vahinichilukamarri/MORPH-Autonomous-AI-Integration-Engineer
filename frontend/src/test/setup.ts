import { cleanup, configure } from '@testing-library/react'
import { afterEach } from 'vitest'

// jsdom has no matchMedia; the app only asks about reduced motion. Tests run as a reduced-motion user, so
// every transition is instant. Node-environment tests have no window.
if (typeof window !== 'undefined' && !window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: query.includes('reduce'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList
}

// Lazy chunks (shell, screens, landing) resolve through dynamic imports, which take longer in jsdom.
configure({ asyncUtilTimeout: 5000 })

afterEach(() => {
  if (typeof window === 'undefined') return
  cleanup()
  window.location.hash = ''
})
