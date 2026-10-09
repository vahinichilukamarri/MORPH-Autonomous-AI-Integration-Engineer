/**
 * Hash routing, so the static build works from any path without server rewrites (GitHub Pages).
 * `#/` is the landing page; `#/app/<screen>` is the product. Links from the first cut (`#/<screen>`)
 * still resolve to the same screen.
 */
import { useSyncExternalStore } from 'react'

import type { IconName } from '../components/Icon'

export type ScreenId = 'overview' | 'pipeline' | 'repair' | 'results' | 'review' | 'policy'

export interface ScreenInfo {
  id: ScreenId
  label: string
  hint: string
  key: string
  icon: IconName
}

export const SCREENS: ScreenInfo[] = [
  { id: 'overview', label: 'Overview', hint: 'What MORPH is and what was measured', key: '1', icon: 'overview' },
  { id: 'pipeline', label: 'Pipeline', hint: 'One unit through every stage', key: '2', icon: 'pipeline' },
  { id: 'repair', label: 'Repair attempts', hint: 'Attempts, feedback, guards and diffs', key: '3', icon: 'repair' },
  { id: 'results', label: 'Results', hint: 'D, L1, L2, L1R, L2R per unit', key: '4', icon: 'results' },
  { id: 'review', label: 'Review gate', hint: 'Unresolved fields block codegen', key: '5', icon: 'review' },
  { id: 'policy', label: 'Policy & audit', hint: 'v0.6 shapes, preview data', key: '6', icon: 'policy' },
]

export type Location = { surface: 'landing'; params: URLSearchParams } | { surface: 'app'; screen: ScreenId; params: URLSearchParams }

const isScreen = (s: string): s is ScreenId => SCREENS.some((r) => r.id === s)

export function parseHash(hash: string): Location {
  const [path, query = ''] = hash.replace(/^#\/?/, '').split('?')
  const params = new URLSearchParams(query)
  const parts = path.split('/').filter(Boolean)
  if (parts[0] === 'app') return { surface: 'app', screen: isScreen(parts[1] ?? '') ? (parts[1] as ScreenId) : 'overview', params }
  if (parts[0] && isScreen(parts[0])) return { surface: 'app', screen: parts[0], params }
  return { surface: 'landing', params }
}

export function href(screen: ScreenId, params?: Record<string, string>): string {
  const query = params ? new URLSearchParams(params).toString() : ''
  return `#/app/${screen}${query ? `?${query}` : ''}`
}

export const LANDING_HREF = '#/'

export function navigate(screen: ScreenId, params?: Record<string, string>): void {
  window.location.hash = href(screen, params)
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener('hashchange', onChange)
  return () => window.removeEventListener('hashchange', onChange)
}

export function useHash(): string {
  return useSyncExternalStore(subscribe, () => window.location.hash)
}

export function useLocation(): Location {
  return parseHash(useHash())
}
