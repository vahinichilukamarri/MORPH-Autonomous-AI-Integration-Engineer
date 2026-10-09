/** Shapes of the generated demo data. Every value is read from a committed file by
 * `scripts/demo-data.ts`; nothing here is typed in by hand. */

export type Condition = 'D' | 'L1' | 'L2' | 'L1R' | 'L2R'
export type InputSet = 'approved' | 'as_proposed'
export type Milestone = 'v0.4' | 'v0.5'

export interface SourceFile {
  path: string
  sha256: string
}

export interface Scenario {
  id: string
  label: string
  source: string
  target: string
  description: string
}

export interface OracleCategory {
  id: string
  passed: number
  total: number
}

export interface Usage {
  calls: number
  input: number
  output: number
  reasoning: number
  /** Provider total; null where the run did not record it (v0.4). */
  total: number | null
  latencyMs: number
}

export interface FeedbackItem {
  stage: string
  code: string
}

export interface Attempt {
  n: number
  /** `replay`: the recorded v0.4 reply served as attempt 0; `network`: a real repair call. */
  source: string
  failedStage: string | null
  feedback: FeedbackItem[]
  guardEnforced: string[]
  guardShadow: string[]
  finishReason: string | null
  usage: Usage
  replyId: string
}

export interface Unit {
  key: string
  scenario: string
  condition: Condition
  inputSet: InputSet
  milestone: Milestone
  status: string
  reason: string | null
  blockedReasons: string[]
  gate: Record<string, boolean>
  gateFindings: string[]
  generatedTests: { passed: number; total: number } | null
  oracle: OracleCategory[] | null
  oracleFailedChecks: string[]
  integrationCorrect: boolean | null
  exposure: string | null
  /** Model use of the unit's own calls (v0.5: repair calls only, attempt 0 excluded). */
  usage: Usage | null
  /** v0.5 only: the recorded v0.4 reply reused as attempt 0. */
  seededUsage: Usage | null
  attempts: Attempt[]
}

export interface ProposedField {
  targetField: string
  proposed: string
  matchesAnswerKey: boolean
}

export interface RunInfo {
  milestone: Milestone
  runDate: string
  model: string
  provider: string
  settings: string | null
  harnessSha: string | null
  postRunEdits: number
  maxRepairAttempts: number | null
  /** v0.5 only: `fixed` means attempt 0 reuses the recorded v0.4 reply. */
  startMode: string | null
}

export interface DemoData {
  generatedFrom: SourceFile[]
  runs: RunInfo[]
  scenarios: Scenario[]
  units: Unit[]
  review: {
    scenario: string
    mode: string
    model: string
    fields: ProposedField[]
  }
}

export interface Reply {
  kind: 'strategy' | 'module'
  /** Pretty-printed strategy JSON (L1) or the module source (L2). */
  text: string
  notes: string | null
}

export interface RepliesData {
  generatedFrom: SourceFile[]
  replies: Record<string, Reply>
}
