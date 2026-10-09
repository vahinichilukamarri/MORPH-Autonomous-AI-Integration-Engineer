/** Read-only views over the recorded units. Every figure the UI shows is computed here or read
 * directly from the generated data; nothing is hard-coded. */
import type { Condition, DemoData, Milestone, Unit, Usage } from './types'

export type Tone = 'ok' | 'fail' | 'human' | 'blocked' | 'incorrect' | 'skipped' | 'accent'

export type Outcome =
  | 'READY_CORRECT'
  | 'READY_INCORRECT'
  | 'READY_UNGRADED'
  | 'HUMAN_REVIEW_REQUIRED'
  | 'BLOCKED_PENDING_REVIEW'
  | 'LLM_INVALID'
  | 'GATE_FAILED'
  | 'OTHER'

export const OUTCOME_TONE: Record<Outcome, Tone> = {
  READY_CORRECT: 'ok',
  READY_INCORRECT: 'incorrect',
  READY_UNGRADED: 'accent',
  HUMAN_REVIEW_REQUIRED: 'human',
  BLOCKED_PENDING_REVIEW: 'blocked',
  LLM_INVALID: 'fail',
  GATE_FAILED: 'fail',
  OTHER: 'skipped',
}

export const OUTCOME_LABEL: Record<Outcome, string> = {
  READY_CORRECT: 'READY · oracle correct',
  READY_INCORRECT: 'READY · incorrect',
  READY_UNGRADED: 'READY · not graded',
  HUMAN_REVIEW_REQUIRED: 'HUMAN_REVIEW_REQUIRED',
  BLOCKED_PENDING_REVIEW: 'BLOCKED_PENDING_REVIEW',
  LLM_INVALID: 'LLM_INVALID',
  GATE_FAILED: 'GATE_FAILED',
  OTHER: 'other',
}

export function outcomeOf(u: Pick<Unit, 'status' | 'integrationCorrect'>): Outcome {
  if (u.status === 'READY') {
    if (u.integrationCorrect === true) return 'READY_CORRECT'
    if (u.integrationCorrect === false) return 'READY_INCORRECT'
    return 'READY_UNGRADED'
  }
  if (
    u.status === 'HUMAN_REVIEW_REQUIRED' ||
    u.status === 'BLOCKED_PENDING_REVIEW' ||
    u.status === 'LLM_INVALID' ||
    u.status === 'GATE_FAILED'
  ) {
    return u.status
  }
  return 'OTHER'
}

export const CONDITIONS: Condition[] = ['D', 'L1', 'L2', 'L1R', 'L2R']

export const CONDITION_INFO: Record<Condition, { name: string; detail: string }> = {
  D: { name: 'Deterministic', detail: 'Compiled from approved mappings with a heuristic strategy. No model.' },
  L1: { name: 'Model strategy', detail: 'The model proposes the sync strategy and edge cases; one shot (v0.4).' },
  L2: { name: 'Model module', detail: 'The model writes the sync module behind the static gate; one shot (v0.4).' },
  L1R: { name: 'Strategy + repair', detail: 'L1 with the bounded repair loop, fixed start (v0.5).' },
  L2R: { name: 'Module + repair', detail: 'L2 with the bounded repair loop, fixed start (v0.5).' },
}

/** Pre-registered exposure tags (docs/plans/v0.5-fixed-start-preregistration.md). */
export const EXPOSURE_INFO: Record<string, string> = {
  PROMPT_GAP: 'The frozen prompt never states a rule the validator enforces.',
  SECRET_LITERAL_FALSE_POSITIVE: 'The AST gate flags benign long strings in generated test data as secrets.',
  CAUSE_CONTRADICTION: 'The L2 task tells the model to use __cause__, which the AST gate bans.',
  MODEL: 'Failure attributed to the model alone.',
}

export function scenarioLabel(data: DemoData, id: string): string {
  return data.scenarios.find((s) => s.id === id)?.label ?? id
}

export function unitTitle(data: DemoData, u: Unit): string {
  const set = u.inputSet === 'approved' ? '' : ' (as proposed)'
  return `${scenarioLabel(data, u.scenario)} ${u.condition}${set}`
}

export function oracleSum(u: Unit): { passed: number; total: number } | null {
  if (!u.oracle) return null
  return u.oracle.reduce((s, c) => ({ passed: s.passed + c.passed, total: s.total + c.total }), {
    passed: 0,
    total: 0,
  })
}

export function repairsUsed(u: Unit): number | null {
  if (u.milestone !== 'v0.5') return null
  return u.attempts.filter((a) => a.source === 'network').length
}

export function approvedUnits(data: DemoData): Unit[] {
  return data.units.filter((u) => u.inputSet === 'approved')
}

export function findUnit(data: DemoData, scenario: string, condition: Condition): Unit | undefined {
  return data.units.find((u) => u.scenario === scenario && u.condition === condition && u.inputSet === 'approved')
}

export interface ConditionSummary {
  condition: Condition
  units: number
  ready: number
  correct: number
  readyIncorrect: number
  humanReview: number
  failed: number
  usage: Usage | null
}

export function summarise(data: DemoData, condition: Condition): ConditionSummary {
  const units = approvedUnits(data).filter((u) => u.condition === condition)
  const outcomes = units.map(outcomeOf)
  const used = units.map((u) => u.usage).filter((x): x is Usage => x !== null)
  return {
    condition,
    units: units.length,
    ready: outcomes.filter((o) => o.startsWith('READY')).length,
    correct: outcomes.filter((o) => o === 'READY_CORRECT').length,
    readyIncorrect: outcomes.filter((o) => o === 'READY_INCORRECT').length,
    humanReview: outcomes.filter((o) => o === 'HUMAN_REVIEW_REQUIRED').length,
    failed: outcomes.filter((o) => o === 'LLM_INVALID' || o === 'GATE_FAILED').length,
    usage: used.length ? addUsage(used) : null,
  }
}

export function addUsage(list: Usage[]): Usage {
  return {
    calls: list.reduce((s, u) => s + u.calls, 0),
    input: list.reduce((s, u) => s + u.input, 0),
    output: list.reduce((s, u) => s + u.output, 0),
    reasoning: list.reduce((s, u) => s + u.reasoning, 0),
    total: list.every((u) => u.total !== null) ? list.reduce((s, u) => s + (u.total ?? 0), 0) : null,
    latencyMs: list.reduce((s, u) => s + u.latencyMs, 0),
  }
}

export const fmt = (n: number): string => n.toLocaleString('en-US')

/** Runs per unit: the largest number of recorded rows for one scenario, condition and input set. */
export function runsPerUnit(data: DemoData): number {
  const counts = new Map<string, number>()
  for (const u of data.units) counts.set(u.key, (counts.get(u.key) ?? 0) + 1)
  return Math.max(...counts.values())
}

/** The persistent badge text for a recorded run: provider, model, runs per unit and start mode. */
export function recordedLabel(data: DemoData, milestone: Milestone = 'v0.5'): string {
  const run = data.runs.find((r) => r.milestone === milestone) ?? data.runs[0]
  const provider = run.provider.charAt(0).toUpperCase() + run.provider.slice(1)
  const mode = run.startMode ? `, ${run.startMode}-start` : ', one-shot'
  return `Recorded run: ${provider} ${run.model}, N=${runsPerUnit(data)}${mode}`
}

export const RUN_NAME: Record<Milestone, string> = {
  'v0.4': 'v0.4 codegen · one-shot',
  'v0.5': 'v0.5 repair · fixed-start',
}

export function runUnits(data: DemoData, milestone: Milestone): Unit[] {
  return data.units.filter((u) => u.milestone === milestone)
}

/** Calls whose provider total equals input + output, i.e. output already contains reasoning. */
export function totalsCheck(data: DemoData): { checked: number; inputPlusOutput: number } {
  const attempts = data.units.flatMap((u) => u.attempts).filter((a) => a.usage.total !== null)
  return {
    checked: attempts.length,
    inputPlusOutput: attempts.filter((a) => a.usage.total === a.usage.input + a.usage.output).length,
  }
}
