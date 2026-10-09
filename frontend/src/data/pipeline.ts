/** The pipeline as stages with a state each, derived from one recorded unit or from a live
 * integration. Pure: the graph component only draws what this returns. */
import { OUTCOME_LABEL, outcomeOf, oracleSum, type Tone } from './derive'
import type { Unit } from './types'

export type StageId =
  | 'discovery'
  | 'mapping'
  | 'review'
  | 'codegen'
  | 'validate'
  | 'gates'
  | 'sandbox'
  | 'repair'
  | 'terminal'
  | 'oracle'

export type StageState = 'pass' | 'fail' | 'blocked' | 'human' | 'incorrect' | 'skipped' | 'loop'

export const STATE_TONE: Record<StageState, Tone> = {
  pass: 'ok',
  fail: 'fail',
  blocked: 'blocked',
  human: 'human',
  incorrect: 'incorrect',
  skipped: 'skipped',
  loop: 'accent',
}

export interface Stage {
  id: StageId
  title: string
  subtitle: string
  state: StageState
  stateLabel: string
  details: string[]
}

export interface Flow {
  id: string
  source: StageId
  target: StageId
  kind: 'main' | 'failure' | 'loop' | 'grade'
  traversed: boolean
}

export interface PipelineView {
  stages: Stage[]
  flows: Flow[]
}

/** Feedback stage to pipeline stage (docs/repair-eval.md: PROPOSAL, GUARD, AST, RUFF, MYPY, TESTS,
 * SMOKE; the gate stops at the first failure). */
const STAGE_OF: Record<string, StageId> = {
  PROPOSAL: 'validate',
  GUARD: 'validate',
  AST: 'gates',
  RUFF: 'gates',
  MYPY: 'gates',
  TESTS: 'sandbox',
  SMOKE: 'sandbox',
}
const STAGE_INDEX: Record<StageId, number> = {
  discovery: 0,
  mapping: 1,
  review: 2,
  codegen: 3,
  validate: 4,
  gates: 5,
  sandbox: 6,
  repair: 7,
  terminal: 8,
  oracle: 9,
}

const LABEL: Record<StageState, string> = {
  pass: 'passed',
  fail: 'failed',
  blocked: 'blocked',
  human: 'needs a person',
  incorrect: 'incorrect',
  skipped: 'not reached',
  loop: 'looped',
}

function stage(id: StageId, title: string, subtitle: string, state: StageState, details: string[]): Stage {
  return { id, title, subtitle, state, stateLabel: LABEL[state], details }
}

/** Where the run stopped: the stage of the first failure, or null when it got through. */
function stopStage(u: Unit): StageId | null {
  if (u.status === 'BLOCKED_PENDING_REVIEW') return 'review'
  if (u.milestone === 'v0.5') {
    const last = u.attempts[u.attempts.length - 1]
    return last?.failedStage ? (STAGE_OF[last.failedStage] ?? 'validate') : null
  }
  if (u.status === 'LLM_INVALID') return 'validate'
  if (u.status === 'GATE_FAILED') return 'gates'
  if (u.status === 'BLOCKED_UNSUPPORTED') return 'codegen'
  return null
}

/** A live version that is generated but not yet tested has not reached the sandbox. */
const isPending = (u: Unit) => u.status.startsWith('GENERATED')

function terminalState(status: string): StageState {
  if (status.startsWith('READY')) return 'pass'
  if (status === 'HUMAN_REVIEW_REQUIRED') return 'human'
  if (status.startsWith('BLOCKED')) return 'blocked'
  if (status.startsWith('GENERATED')) return 'skipped'
  return 'fail'
}

function stateFor(id: StageId, stop: StageId | null, failState: StageState): StageState {
  if (stop === null) return 'pass'
  if (STAGE_INDEX[id] < STAGE_INDEX[stop]) return 'pass'
  if (id === stop) return failState
  return 'skipped'
}

export function pipelineFromUnit(u: Unit): PipelineView {
  const stop = stopStage(u)
  const outcome = outcomeOf(u)
  const isRepair = u.milestone === 'v0.5'
  const failedStages = new Set(
    u.attempts.filter((a) => a.failedStage).map((a) => STAGE_OF[a.failedStage ?? ''] ?? 'validate'),
  )
  const repairs = u.attempts.filter((a) => a.source === 'network').length
  const blocked = stop === 'review'
  const sum = oracleSum(u)

  const validateDetails = isRepair
    ? u.attempts
        .filter((a) => a.failedStage === 'PROPOSAL' || a.failedStage === 'GUARD')
        .map((a) => `Attempt ${a.n}: ${a.feedback.map((f) => `${f.stage}.${f.code}`).join(', ')}`)
    : u.status === 'LLM_INVALID'
      ? ['The proposal was invalid after the one re-ask.']
      : []
  const gateDetails = isRepair
    ? u.attempts
        .filter((a) => STAGE_OF[a.failedStage ?? ''] === 'gates')
        .map((a) => `Attempt ${a.n}: ${a.feedback.map((f) => `${f.stage}.${f.code}`).join(', ')}`)
    : [
        ...Object.entries(u.gate).map(([k, v]) => `${k}: ${v ? 'pass' : 'fail'}`),
        ...u.gateFindings,
      ]
  const sandboxDetails = u.generatedTests
    ? [`Generated tests ${u.generatedTests.passed}/${u.generatedTests.total} passed.`]
    : isRepair && stop === null
      ? ['Generated tests and the smoke test passed.']
      : []

  const stages: Stage[] = [
    stage('discovery', 'Discovery', 'OpenAPI to system model', 'pass', [
      'Both contracts are parsed into the internal system model and embedded in pgvector.',
    ]),
    stage(
      'mapping',
      'Mapping',
      u.inputSet === 'approved' ? 'Approved mappings' : 'Model proposals',
      'pass',
      [
        u.inputSet === 'approved'
          ? 'Reference pipelines stored as human-approved mappings, so codegen is measured apart from mapping quality.'
          : 'The real v0.3 model proposals, with no human edit.',
      ],
    ),
    stage(
      'review',
      'Review gate',
      blocked ? 'BLOCKED_PENDING_REVIEW' : 'Required fields approved',
      blocked ? 'blocked' : 'pass',
      blocked ? u.blockedReasons : ['Every required, writable target field is approved.'],
    ),
    stage(
      'codegen',
      'Codegen',
      `Condition ${u.condition}`,
      blocked ? 'skipped' : stateFor('codegen', stop, 'blocked'),
      blocked
        ? ['No code was generated and no model was called.']
        : [
            u.condition === 'D'
              ? 'Deterministic compile and heuristic strategy, no model.'
              : u.condition.startsWith('L1')
                ? 'The model proposes the sync strategy and edge records.'
                : 'The model writes the sync module.',
          ],
    ),
    stage(
      'validate',
      isRepair ? 'Validation + guards' : 'Validation',
      isRepair ? 'Proposal rules, G1 to G5' : 'Proposal rules',
      stateFor('validate', stop, 'fail'),
      validateDetails,
    ),
    stage('gates', 'Static gate', 'AST, ruff, mypy', stateFor('gates', stop, 'fail'), gateDetails),
    stage(
      'sandbox',
      'Sandbox',
      'Generated tests, smoke',
      isPending(u) ? 'skipped' : stateFor('sandbox', stop, 'fail'),
      sandboxDetails,
    ),
    stage(
      'repair',
      'Repair loop',
      isRepair ? `${repairs} repair${repairs === 1 ? '' : 's'} used` : 'Not in this run',
      isRepair ? (repairs > 0 ? 'loop' : 'pass') : 'skipped',
      isRepair
        ? u.attempts.map(
            (a) =>
              `Attempt ${a.n} (${a.source === 'network' ? 'repair call' : 'seeded v0.4 reply'}): ${a.failedStage ?? 'READY'}`,
          )
        : ['v0.4 ran each unit once, with no repair loop.'],
    ),
    stage(
      'terminal',
      'Outcome',
      u.status === 'HUMAN_REVIEW_REQUIRED' && u.reason ? `${u.status} (${u.reason})` : u.status,
      terminalState(u.status),
      [OUTCOME_LABEL[outcome], ...(u.exposure ? [`Exposure tag: ${u.exposure}`] : [])],
    ),
    stage(
      'oracle',
      'Hidden oracle',
      'Bench only, never seen by repair',
      u.integrationCorrect === true ? 'pass' : u.integrationCorrect === false ? 'incorrect' : 'skipped',
      sum
        ? [
            `${sum.passed}/${sum.total} checks passed (O1 to O7).`,
            ...u.oracleFailedChecks.map((c) => `Failed: ${c}`),
          ]
        : ['Not graded: only READY units are graded.'],
    ),
  ]

  const reached = (id: StageId) => stages.find((s) => s.id === id)?.state !== 'skipped'
  const main: [StageId, StageId][] = [
    ['discovery', 'mapping'],
    ['mapping', 'review'],
    ['review', 'codegen'],
    ['codegen', 'validate'],
    ['validate', 'gates'],
    ['gates', 'sandbox'],
    ['sandbox', 'terminal'],
  ]
  const flows: Flow[] = main.map(([source, target]) => ({
    id: `${source}-${target}`,
    source,
    target,
    kind: 'main',
    traversed: reached(source) && reached(target) && !(source === 'sandbox' && stop !== null),
  }))
  for (const from of ['validate', 'gates', 'sandbox'] as StageId[]) {
    flows.push({
      id: `${from}-repair`,
      source: from,
      target: 'repair',
      kind: 'failure',
      traversed: isRepair && failedStages.has(from),
    })
  }
  flows.push(
    { id: 'repair-codegen', source: 'repair', target: 'codegen', kind: 'loop', traversed: repairs > 0 },
    {
      id: 'repair-terminal',
      source: 'repair',
      target: 'terminal',
      kind: 'failure',
      traversed: isRepair && u.status === 'HUMAN_REVIEW_REQUIRED',
    },
    { id: 'review-terminal', source: 'review', target: 'terminal', kind: 'failure', traversed: blocked },
    {
      id: 'stop-terminal',
      source: stop && stop !== 'review' ? stop : 'validate',
      target: 'terminal',
      kind: 'failure',
      traversed: !isRepair && stop !== null && stop !== 'review',
    },
    { id: 'terminal-oracle', source: 'terminal', target: 'oracle', kind: 'grade', traversed: u.integrationCorrect !== null },
  )
  return { stages, flows }
}
