/** Live mode: reads one integration version from the REST API into the same `Unit` shape the
 * recorded data uses, so the pipeline view draws both the same way. Live runs have no oracle. */
import type { Condition, Unit } from '../data/types'
import { rest } from './rest'

const CONDITIONS: Condition[] = ['D', 'L1', 'L2', 'L1R', 'L2R']

function finding(f: unknown): string {
  if (typeof f === 'string') return f
  if (f && typeof f === 'object') {
    const o = f as Record<string, unknown>
    const parts = [o.code, o.message, o.path].filter((x) => typeof x === 'string' && x)
    if (parts.length) return parts.join(' · ')
  }
  return JSON.stringify(f)
}

export async function loadLiveUnit(integrationId: number, versionNumber: number | null, signal: AbortSignal): Promise<Unit> {
  const integration = await rest.integration(integrationId, signal)
  const n = versionNumber ?? Math.max(...integration.versions)
  if (!integration.versions.includes(n)) throw new Error(`integration ${integrationId} has no version ${n}`)
  const [version, gates, runs] = await Promise.all([
    rest.version(integrationId, n, signal),
    rest.gate(integrationId, n, signal),
    rest.sandboxRuns(integrationId, n, signal),
  ])
  const tests = runs.find((r) => r.purpose === 'generated_tests')
  const condition = CONDITIONS.find((c) => c === integration.condition) ?? 'D'
  const gateFindings = gates.flatMap((g) => g.findings.map((f) => `${g.stage}: ${finding(f)}`))
  if (tests) gateFindings.push(`sandbox generated tests: ${tests.outcome}`)
  return {
    key: `live:${integrationId}:${n}`,
    scenario: integration.name,
    condition,
    inputSet: 'approved',
    milestone: 'v0.4',
    status: version.status,
    reason: version.plan_error,
    blockedReasons: (version.review?.blocking ?? []).map((b) => `${b.target_field}: ${b.reason}`),
    gate: Object.fromEntries(gates.map((g) => [g.stage, g.passed])),
    gateFindings,
    generatedTests: null,
    oracle: null,
    oracleFailedChecks: [],
    integrationCorrect: null,
    exposure: null,
    usage: null,
    seededUsage: null,
    attempts: [],
  }
}
