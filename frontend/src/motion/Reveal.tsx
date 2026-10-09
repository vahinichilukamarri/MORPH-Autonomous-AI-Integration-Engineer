import * as m from 'motion/react-m'
import type { ReactNode } from 'react'

import { useReducedMotion } from '../app/theme'
import { DURATION, EASE } from './tokens'

/** Lifts its content into place the first time it scrolls into view. Only transform animates, never
 * opacity, so content is readable before, during and without the animation (print, screenshots, a jump
 * to an anchor). With reduced motion it is simply shown. */
export function Reveal({
  children,
  delay = 0,
  className,
  as = 'div',
}: {
  children: ReactNode
  delay?: number
  className?: string
  as?: 'div' | 'li' | 'section'
}) {
  const reduced = useReducedMotion()
  const Component = m[as]
  if (reduced) return <Component className={className}>{children}</Component>
  return (
    <Component
      className={className}
      initial={{ y: 28, scale: 0.985 }}
      whileInView={{ y: 0, scale: 1 }}
      viewport={{ once: true, margin: '0px 0px -10% 0px' }}
      transition={{ duration: DURATION.slow, ease: EASE.out, delay }}
    >
      {children}
    </Component>
  )
}
