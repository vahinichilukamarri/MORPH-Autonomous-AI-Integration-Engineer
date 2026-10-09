/**
 * The run selector's state: which recorded run the app is scoped to. A per-tab view setting, kept in
 * memory and in `sessionStorage` when it is available.
 */
import { useSyncExternalStore } from 'react'

import type { Milestone } from '../data/types'

const KEY = 'morph.run'
const listeners = new Set<() => void>()

function initial(): Milestone {
  try {
    return window.sessionStorage.getItem(KEY) === 'v0.4' ? 'v0.4' : 'v0.5'
  } catch {
    return 'v0.5'
  }
}

let current: Milestone | null = null

export function getRun(): Milestone {
  current ??= initial()
  return current
}

export function setRun(run: Milestone): void {
  current = run
  try {
    window.sessionStorage.setItem(KEY, run)
  } catch {
    // Storage blocked: the choice still holds for this page.
  }
  listeners.forEach((l) => l())
}

export function useRun(): Milestone {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l)
      return () => listeners.delete(l)
    },
    getRun,
  )
}
