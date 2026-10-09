import { lazy, Suspense, useEffect } from 'react'

import { ErrorBoundary } from '../components/ErrorBoundary'
import { ToastProvider } from '../components/Toast'
import { MotionProvider } from '../motion/MotionProvider'
import { useLocation } from './router'

const Landing = lazy(() => import('../landing/Landing'))
const AppShell = lazy(() => import('./AppShell'))

/** Chooses the surface: the landing page at `#/`, the product shell at `#/app/...`. Each is its own chunk. */
export function Root() {
  const location = useLocation()
  const surface = location.surface

  useEffect(() => {
    document.body.dataset.surface = surface
  }, [surface])

  return (
    <MotionProvider>
      <ToastProvider>
        <ErrorBoundary>
          {/* No placeholder while the first chunk loads: a skeleton swapped for the page would shift layout. */}
          <Suspense fallback={null}>
            {location.surface === 'landing' ? <Landing /> : <AppShell screen={location.screen} params={location.params} />}
          </Suspense>
        </ErrorBoundary>
      </ToastProvider>
    </MotionProvider>
  )
}
