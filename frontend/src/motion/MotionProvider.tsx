import { LazyMotion, MotionConfig } from 'motion/react'
import type { ReactNode } from 'react'

import { useReducedMotion } from '../app/theme'
import { DURATION, EASE } from './tokens'

const loadFeatures = () => import('./features').then((m) => m.default)

/**
 * One place for motion policy. Under `prefers-reduced-motion` every transition is instant, so content
 * appears in its final state and nothing waits on an animation; otherwise the shared tokens apply.
 * Until the features chunk arrives, `m` components render in their final state, unanimated.
 */
export function MotionProvider({ children }: { children: ReactNode }) {
  const reduced = useReducedMotion()
  return (
    <LazyMotion features={loadFeatures} strict>
      <MotionConfig
        reducedMotion={reduced ? 'always' : 'never'}
        transition={reduced ? { duration: 0 } : { duration: DURATION.base, ease: EASE.out }}
      >
        {children}
      </MotionConfig>
    </LazyMotion>
  )
}
