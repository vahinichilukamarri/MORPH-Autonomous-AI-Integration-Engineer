/** Hash routing, so the static build works from any path without server rewrites. */
import { useSyncExternalStore } from 'react'

export type RouteId = 'overview' | 'pipeline' | 'repair' | 'results' | 'review' | 'policy'

export interface RouteInfo {
  id: RouteId
  label: string
  hint: string
  key: string
}

export const ROUTES: RouteInfo[] = [
  { id: 'overview', label: 'Overview', hint: 'What MORPH is and what was measured', key: '1' },
  { id: 'pipeline', label: 'Pipeline', hint: 'Stages of one unit, with state', key: '2' },
  { id: 'repair', label: 'Repair', hint: 'Attempts, feedback and diffs', key: '3' },
  { id: 'results', label: 'Results', hint: 'D, L1, L2, L1R, L2R per unit', key: '4' },
  { id: 'review', label: 'Review gate', hint: 'Unresolved fields block codegen', key: '5' },
  { id: 'policy', label: 'Policy & audit', hint: 'v0.6 shapes, fixture data', key: '6' },
]

export interface Location {
  route: RouteId
  params: URLSearchParams
}

export function parseHash(hash: string): Location {
  const [path, query = ''] = hash.replace(/^#\/?/, '').split('?')
  const route = ROUTES.find((r) => r.id === path)?.id ?? 'overview'
  return { route, params: new URLSearchParams(query) }
}

export function href(route: RouteId, params?: Record<string, string>): string {
  const query = params ? new URLSearchParams(params).toString() : ''
  return `#/${route}${query ? `?${query}` : ''}`
}

export function navigate(route: RouteId, params?: Record<string, string>): void {
  window.location.hash = href(route, params)
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener('hashchange', onChange)
  return () => window.removeEventListener('hashchange', onChange)
}

export function useLocation(): Location {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash)
  return parseHash(hash)
}
