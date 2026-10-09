/** Motion tokens, mirrored from the CSS custom properties in src/styles/tokens.css (a test checks they
 * agree). Durations are in seconds, as the motion library expects. */
export const DURATION = {
  fast: 0.12,
  base: 0.2,
  slow: 0.36,
} as const

export const EASE = {
  out: [0.2, 0.8, 0.2, 1],
  inOut: [0.65, 0, 0.35, 1],
} as const

/** The standard entrance: a short rise with a fade, used by pages, reveals and list items. */
export const ENTER = {
  initial: { opacity: 0, y: 8 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: DURATION.slow, ease: EASE.out },
} as const

/** Delay between items of a staggered list. Kept short so a long list never holds content back. */
export const STAGGER = 0.04
