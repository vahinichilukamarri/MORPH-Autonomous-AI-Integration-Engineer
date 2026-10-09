/**
 * Builds the demo-mode data from committed files only. Pure apart from reading those files, so the
 * drift test can rebuild it and compare with what is committed under `src/data/`.
 */
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import type {
  Attempt,
  Condition,
  DemoData,
  FeedbackItem,
  InputSet,
  OracleCategory,
  ProposedField,
  RepliesData,
  Reply,
  RunInfo,
  Scenario,
  SourceFile,
  Unit,
  Usage,
} from '../src/data/types.ts'

export const SOURCES = {
  codegenResults: 'bench/results/codegen-v0.4/results.jsonl',
  codegenProvenance: 'bench/results/codegen-v0.4/provenance.json',
  codegenReplies: 'bench/replays/codegen/calls.jsonl',
  repairResults: 'bench/results/repair-v0.5/fixed/results.jsonl',
  repairProvenance: 'bench/results/repair-v0.5/fixed/provenance.json',
  repairReplies: 'bench/replays/repair/fixed/calls.jsonl',
  repairExpected: 'bench/replays/repair/fixed/expected.json',
  mappingReplay: 'bench/replays/crm_customer_to_support_user.expected.json',
  repairState: 'backend/app/repair/state.py',
} as const

/** Naming used throughout the evaluation reports (docs/codegen-eval.md, docs/repair-eval.md). */
const SCENARIO_LABELS: Record<string, string> = {
  crm_customer_to_support_user: 'S1',
  support_user_to_crm_customer: 'S3',
  crm_v2_to_support_v2: 'S4',
}

type Json = Record<string, unknown>

class Reader {
  readonly used: SourceFile[] = []
  private readonly root: string

  constructor(root: string) {
    this.root = root
  }

  text(path: string): string {
    // Hash with LF endings, as stored in git, so a Windows checkout gives the same digest.
    const content = readFileSync(join(this.root, path), 'utf8').replace(/\r\n/g, '\n')
    if (!this.used.some((s) => s.path === path)) {
      this.used.push({ path, sha256: createHash('sha256').update(content).digest('hex') })
    }
    return content
  }

  json(path: string): Json {
    return JSON.parse(this.text(path)) as Json
  }

  jsonl(path: string): Json[] {
    return this.text(path)
      .split('\n')
      .filter((line) => line.trim() !== '')
      .map((line) => JSON.parse(line) as Json)
  }
}

const str = (v: unknown): string => {
  if (typeof v !== 'string') throw new Error(`expected a string, got ${JSON.stringify(v)}`)
  return v
}
const num = (v: unknown): number => {
  if (typeof v !== 'number') throw new Error(`expected a number, got ${JSON.stringify(v)}`)
  return v
}
const optNum = (v: unknown): number | null => (v === null || v === undefined ? null : num(v))
const optStr = (v: unknown): string | null => (v === null || v === undefined || v === '' ? null : str(v))
const strList = (v: unknown): string[] => (Array.isArray(v) ? v.map(str) : [])
const obj = (v: unknown): Json => {
  if (typeof v !== 'object' || v === null || Array.isArray(v)) throw new Error('expected an object')
  return v as Json
}

function oracle(v: unknown): OracleCategory[] | null {
  if (v === null || v === undefined) return null
  return Object.entries(obj(v)).map(([id, pair]) => {
    const [passed, total] = pair as [number, number]
    return { id, passed: num(passed), total: num(total) }
  })
}

function feedback(codes: string[]): FeedbackItem[] {
  return codes.map((c) => {
    const dot = c.indexOf('.')
    return { stage: c.slice(0, dot), code: c.slice(dot + 1) }
  })
}

function sumUsage(attempts: Attempt[]): Usage {
  const totals = attempts.map((a) => a.usage.total)
  return {
    calls: attempts.length,
    input: attempts.reduce((s, a) => s + a.usage.input, 0),
    output: attempts.reduce((s, a) => s + a.usage.output, 0),
    reasoning: attempts.reduce((s, a) => s + a.usage.reasoning, 0),
    total: totals.every((t) => t !== null) ? totals.reduce<number>((s, t) => s + (t ?? 0), 0) : null,
    latencyMs: attempts.reduce((s, a) => s + a.usage.latencyMs, 0),
  }
}

function scenarios(reader: Reader, ids: string[]): Scenario[] {
  return ids.map((id) => {
    const yaml = reader.text(`bench/scenarios/${id}/scenario.yaml`)
    const side = (key: string): string => {
      const m = new RegExp(`^${key}:\\n  system: (\\S+)\\n  entity: (\\S+)`, 'm').exec(yaml)
      if (!m) throw new Error(`${id}: no ${key} in scenario.yaml`)
      return `${m[1]}.${m[2]}`
    }
    const desc = /^description: >-\n((?: {2}.*\n)+)/m.exec(yaml)
    if (!desc) throw new Error(`${id}: no description in scenario.yaml`)
    const label = SCENARIO_LABELS[id]
    if (!label) throw new Error(`${id}: no report label`)
    return {
      id,
      label,
      source: side('source'),
      target: side('target'),
      description: desc[1]
        .split('\n')
        .map((l) => l.trim())
        .filter(Boolean)
        .join(' '),
    }
  })
}

function codegenUnits(reader: Reader): Unit[] {
  return reader.jsonl(SOURCES.codegenResults).map((r) => {
    const llm = r.llm === null ? null : obj(r.llm)
    const tests = r.generated_tests === null ? null : obj(r.generated_tests)
    const condition = str(r.condition) as Condition
    const inputSet = str(r.input_set) as InputSet
    return {
      key: `${str(r.scenario_id)}:${condition}:${inputSet}`,
      scenario: str(r.scenario_id),
      condition,
      inputSet,
      milestone: 'v0.4',
      status: str(r.status),
      reason: null,
      blockedReasons: strList(r.blocked_reasons),
      gate: Object.fromEntries(Object.entries(obj(r.gate)).map(([k, v]) => [k, Boolean(v)])),
      gateFindings: strList(r.gate_findings),
      generatedTests: tests ? { passed: num(tests.passed), total: num(tests.total) } : null,
      oracle: oracle(r.oracle),
      oracleFailedChecks: strList(r.oracle_failed_checks),
      integrationCorrect: r.integration_correct === null ? null : Boolean(r.integration_correct),
      exposure: null,
      usage: llm
        ? {
            calls: num(llm.calls),
            input: num(llm.input_tokens),
            output: num(llm.output_tokens),
            reasoning: num(llm.reasoning_tokens),
            total: null,
            latencyMs: num(llm.latency_ms),
          }
        : null,
      seededUsage: null,
      attempts: [],
    }
  })
}

function repairUnits(reader: Reader): Unit[] {
  const expected = JSON.parse(reader.text(SOURCES.repairExpected)) as Json[]
  return reader.jsonl(SOURCES.repairResults).map((r) => {
    const attempts: Attempt[] = (r.attempts as Json[]).map((a) => ({
      n: num(a.attempt),
      source: str(a.source),
      failedStage: optStr(a.failed_stage),
      feedback: feedback(strList(a.feedback_codes)),
      guardEnforced: strList(a.guard_enforced),
      guardShadow: strList(a.guard_shadow),
      finishReason: optStr(a.finish_reason),
      usage: {
        calls: 1,
        input: num(a.input_tokens),
        output: num(a.output_tokens),
        reasoning: num(a.reasoning_tokens),
        total: optNum(a.total_tokens),
        latencyMs: num(a.latency_ms),
      },
      replyId: str(a.prompt_hash),
    }))
    const scenario = str(r.scenario_id)
    const condition = str(r.condition) as Condition
    // The replay test's expectation must agree with the results file, stage for stage.
    const exp = expected.find((e) => e.scenario_id === scenario && e.condition === condition)
    const expStages = exp ? (exp.attempts as Json[]).map((a) => a.failed_stage ?? null) : null
    if (JSON.stringify(expStages) !== JSON.stringify(attempts.map((a) => a.failedStage))) {
      throw new Error(`${scenario} ${condition}: results and expected.json disagree`)
    }
    const repairs = attempts.filter((a) => a.source === 'network')
    const seeded = attempts.filter((a) => a.source !== 'network')
    return {
      key: `${scenario}:${condition}:approved`,
      scenario,
      condition,
      inputSet: 'approved',
      milestone: 'v0.5',
      status: str(r.status),
      reason: optStr(r.reason),
      blockedReasons: [],
      gate: {},
      gateFindings: [],
      generatedTests: null,
      oracle: oracle(r.oracle),
      oracleFailedChecks: strList(r.oracle_failed_checks),
      integrationCorrect: r.integration_correct === null ? null : Boolean(r.integration_correct),
      exposure: optStr(r.exposure),
      usage: repairs.length ? sumUsage(repairs) : null,
      seededUsage: seeded.length ? sumUsage(seeded) : null,
      attempts,
    }
  })
}

function runs(reader: Reader): RunInfo[] {
  const cg = reader.json(SOURCES.codegenProvenance)
  const cgRun = obj(cg.run)
  const rp = reader.json(SOURCES.repairProvenance)
  const state = reader.text(SOURCES.repairState)
  const max = /^MAX_REPAIR_ATTEMPTS = (\d+)/m.exec(state)
  if (!max) throw new Error('MAX_REPAIR_ATTEMPTS not found')
  const modes = [...new Set(reader.jsonl(SOURCES.repairResults).map((r) => str(r.start_mode)))]
  return [
    {
      milestone: 'v0.4',
      runDate: str(cgRun.date),
      model: str(cgRun.model_reported_by_replies),
      provider: str(cgRun.provider),
      settings: optStr(cgRun.settings),
      harnessSha: str(cgRun.harness_commit).split(' ')[0],
      postRunEdits: (cg.post_run_edits as unknown[]).length,
      maxRepairAttempts: null,
      startMode: null,
    },
    {
      milestone: 'v0.5',
      runDate: str(rp.run_date),
      model: str(rp.model_reported_by_replies),
      provider: str(rp.provider),
      settings: optStr(rp.settings),
      harnessSha: str(rp.harness_sha).slice(0, 7),
      postRunEdits: (rp.post_run_edits as unknown[]).length,
      maxRepairAttempts: Number(max[1]),
      startMode: modes.length === 1 ? modes[0] : null,
    },
  ]
}

function review(reader: Reader): DemoData['review'] {
  const replay = reader.json(SOURCES.mappingReplay)
  const mode = 'full_schema'
  const fields: ProposedField[] = (replay[mode] as Json[]).map((f) => ({
    targetField: str(f.target_field),
    proposed: str(f.proposed),
    matchesAnswerKey: Boolean(f.fully_correct),
  }))
  return { scenario: 'crm_customer_to_support_user', mode, model: str(replay.model), fields }
}

function reply(text: string): Reply {
  const parsed = JSON.parse(text) as Json
  if (typeof parsed.source === 'string') {
    return { kind: 'module', text: parsed.source, notes: optStr(parsed.notes) }
  }
  return { kind: 'strategy', text: JSON.stringify(parsed, null, 2), notes: null }
}

export function buildDemoData(root: string): { demo: DemoData; replies: RepliesData } {
  const reader = new Reader(root)
  const units = [...codegenUnits(reader), ...repairUnits(reader)]
  const ids = [...new Set(units.map((u) => u.scenario))].sort(
    (a, b) => (SCENARIO_LABELS[a] ?? a).localeCompare(SCENARIO_LABELS[b] ?? b),
  )
  const demo: DemoData = {
    generatedFrom: [],
    runs: runs(reader),
    scenarios: scenarios(reader, ids),
    units,
    review: review(reader),
  }
  demo.generatedFrom = [...reader.used]

  const replyReader = new Reader(root)
  const wanted = new Set(units.flatMap((u) => u.attempts.map((a) => a.replyId)))
  const replies: Record<string, Reply> = {}
  for (const path of [SOURCES.codegenReplies, SOURCES.repairReplies]) {
    for (const call of replyReader.jsonl(path)) {
      const id = str(call.prompt_hash)
      if (wanted.has(id)) replies[id] = reply(str(call.text))
    }
  }
  const missing = [...wanted].filter((id) => !(id in replies))
  if (missing.length) throw new Error(`replies missing for ${missing.length} attempts`)
  return { demo, replies: { generatedFrom: [...replyReader.used], replies } }
}
