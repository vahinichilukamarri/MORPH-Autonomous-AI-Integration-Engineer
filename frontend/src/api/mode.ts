export type Mode = 'demo' | 'live'

/** `vite --mode live` (npm run dev:live, build:live) or VITE_MORPH_MODE=live; anything else is the
 * static demo, which needs no backend. */
export const MODE: Mode =
  import.meta.env.MODE === 'live' || import.meta.env.VITE_MORPH_MODE === 'live' ? 'live' : 'demo'
