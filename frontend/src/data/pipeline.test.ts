import { describe, expect, it } from 'vitest'

import { pipelineFromUnit, type PipelineView, type StageId } from './pipeline'
import { recorded } from './recorded'
import type { Unit } from './types'

function pick(predicate: (u: Unit) => boolean, what: string): Unit {
  const u = recorded.units.find(predicate)
  if (!u) throw new Error(`the recorded data has no ${what}`)
  return u
}
const state = (view: PipelineView, id: StageId) => view.stages.find((s) => s.id === id)?.state
const flow = (view: PipelineView, id: string) => view.flows.find((f) => f.id === id)?.traversed

describe('pipeline states derived from recorded units', () => {
  it('READY but incorrect shows a passing run and an incorrect oracle', () => {
    const view = pipelineFromUnit(pick((u) => u.status === 'READY' && u.integrationCorrect === false, 'READY-but-incorrect unit'))
    expect(state(view, 'terminal')).toBe('pass')
    expect(state(view, 'oracle')).toBe('incorrect')
    expect(flow(view, 'repair-codegen')).toBe(true)
  })

  it('a unit that exhausted its repairs ends with a person', () => {
    const view = pipelineFromUnit(pick((u) => u.status === 'HUMAN_REVIEW_REQUIRED', 'HUMAN_REVIEW_REQUIRED unit'))
    expect(state(view, 'terminal')).toBe('human')
    expect(state(view, 'oracle')).toBe('skipped')
    expect(flow(view, 'repair-terminal')).toBe(true)
  })

  it('an as-proposed unit stops at the review gate with nothing generated', () => {
    const view = pipelineFromUnit(pick((u) => u.inputSet === 'as_proposed', 'as-proposed unit'))
    expect(state(view, 'review')).toBe('blocked')
    expect(state(view, 'codegen')).toBe('skipped')
    expect(state(view, 'sandbox')).toBe('skipped')
    expect(flow(view, 'review-terminal')).toBe(true)
    expect(flow(view, 'review-codegen')).toBe(false)
  })

  it('a v0.4 gate failure stops at the static gate and has no repair loop', () => {
    const view = pipelineFromUnit(pick((u) => u.milestone === 'v0.4' && u.status === 'GATE_FAILED', 'v0.4 GATE_FAILED unit'))
    expect(state(view, 'gates')).toBe('fail')
    expect(state(view, 'sandbox')).toBe('skipped')
    expect(state(view, 'repair')).toBe('skipped')
  })

  it('condition D on approved inputs passes every stage and the oracle', () => {
    const view = pipelineFromUnit(pick((u) => u.condition === 'D' && u.inputSet === 'approved', 'approved D unit'))
    for (const id of ['review', 'codegen', 'validate', 'gates', 'sandbox', 'terminal', 'oracle'] as StageId[]) {
      expect(state(view, id)).toBe('pass')
    }
  })
})
