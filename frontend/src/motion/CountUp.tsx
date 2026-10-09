import { useEffect, useRef, useState } from 'react'

import { useReducedMotion } from '../app/theme'
import { DURATION } from './tokens'

const easeOut = (t: number) => 1 - (1 - t) ** 3

/**
 * Counts up to `value` the first time it is seen. Only for numbers read from the generated data. The
 * final value is what assistive technology reads from the start (the moving digits are aria-hidden),
 * and with reduced motion, or without IntersectionObserver, the final value is shown directly.
 */
export function CountUp({ value, format = String }: { value: number; format?: (n: number) => string }) {
  const ref = useRef<HTMLSpanElement>(null)
  const reduced = useReducedMotion()
  const [frame, setFrame] = useState<number | null>(null)
  const animated = !reduced && value > 0 && typeof IntersectionObserver !== 'undefined'

  useEffect(() => {
    const el = ref.current
    if (!animated || !el) return
    let raf = 0
    const run = () => {
      const start = performance.now()
      const total = DURATION.slow * 2500
      const tick = (now: number) => {
        const t = Math.min((now - start) / total, 1)
        setFrame(t >= 1 ? null : Math.round(easeOut(t) * value))
        if (t < 1) raf = requestAnimationFrame(tick)
      }
      raf = requestAnimationFrame(tick)
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        observer.disconnect()
        run()
      }
    })
    observer.observe(el)
    return () => {
      observer.disconnect()
      cancelAnimationFrame(raf)
    }
  }, [animated, value])

  return (
    <span className="count" ref={ref}>
      <span className="visually-hidden">{format(value)}</span>
      <span aria-hidden="true">{format(frame ?? value)}</span>
    </span>
  )
}
