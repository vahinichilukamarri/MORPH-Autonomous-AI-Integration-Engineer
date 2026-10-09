/**
 * FIXTURE: illustrative policy, audit and approval data in the shapes of the v0.6 plan.
 * Not a recorded run and not produced by MORPH. The v0.6 policy layer and its REST endpoints
 * (GET /policy/active, /audit/events, /audit/verify, /approvals, POST /approvals/{id}/decide)
 * do not exist on this branch yet. The hash chain is computed in the browser over these fixture rows, so the
 * verification is real but only shows that the fixture is self-consistent.
 */
import type {
  ActivePolicy,
  Approval,
  AuditEvent,
  AuditEventType,
  Decision,
  PolicyApi,
  PolicyRule,
} from '../api/policyTypes'

const POLICY_HASH = 'preview-policy-hash'

function rule(
  id: string,
  tools: string[],
  outcome: PolicyRule['outcome'],
  reasonCode: string,
  scope: Partial<Pick<PolicyRule, 'roles' | 'environments' | 'dataClasses'>> = {},
): PolicyRule {
  return {
    id,
    tools,
    roles: scope.roles ?? ['operator'],
    environments: scope.environments ?? ['any'],
    dataClasses: scope.dataClasses ?? ['any'],
    outcome,
    reasonCode,
  }
}

const POLICY: ActivePolicy = {
  version: 'v1 (preview)',
  policyHash: POLICY_HASH,
  file: 'backend/policy/morph-policy-v1.yaml (planned)',
  rules: [
    rule(
      'read.any',
      ['list_systems', 'get_system', 'get_mapping_run', 'get_integration', 'get_repair_run', 'describe_policy'],
      'allow',
      'READ_ONLY',
      { roles: ['reader', 'operator'] },
    ),
    rule('ingest.local', ['ingest_contract'], 'allow', 'LOCAL_FILE_UNDER_SPEC_ROOT'),
    rule('model.synthetic', ['propose_mapping', 'generate_integration'], 'allow', 'SYNTHETIC_WITHIN_BUDGET', {
      dataClasses: ['synthetic'],
    }),
    rule('model.other', ['propose_mapping', 'generate_integration'], 'needs_approval', 'MODEL_CALL_ON_NON_SYNTHETIC_DATA', {
      dataClasses: ['internal', 'restricted'],
    }),
    rule('tests.mock', ['run_generated_tests'], 'allow', 'MOCK_TARGET', { environments: ['mock'] }),
    rule('repair.approval', ['repair_integration'], 'needs_approval', 'REPAIR_NEEDS_APPROVAL', {
      environments: ['mock'],
    }),
  ],
  floor: [
    { id: 'floor.execute_mock_only', description: 'Execute tools run only against mock targets.' },
    { id: 'floor.no_url_fetch', description: 'Fetching a URL is denied.' },
    { id: 'floor.no_secrets', description: 'No secret value leaves the server.' },
    { id: 'floor.no_self_approval', description: 'No tool can approve, change policy, grade or bypass the review gate.' },
    { id: 'floor.result_cap', description: 'Tool results are size capped.' },
  ],
}

interface Row {
  eventType: AuditEventType
  tool: string
  callId: string
  decision?: Decision
  reasonCode?: string
  ruleIds?: string[]
}

const ROWS: Row[] = [
  { eventType: 'CALL_RECEIVED', tool: 'ingest_contract', callId: 'call-a' },
  { eventType: 'POLICY_DECISION', tool: 'ingest_contract', callId: 'call-a', decision: 'ALLOW', reasonCode: 'LOCAL_FILE_UNDER_SPEC_ROOT', ruleIds: ['ingest.local'] },
  { eventType: 'CALL_EXECUTED', tool: 'ingest_contract', callId: 'call-a' },
  { eventType: 'CALL_RECEIVED', tool: 'ingest_contract', callId: 'call-b' },
  { eventType: 'POLICY_DECISION', tool: 'ingest_contract', callId: 'call-b', decision: 'DENY', reasonCode: 'FLOOR_URL_FETCH', ruleIds: ['floor.no_url_fetch'] },
  { eventType: 'CALL_RECEIVED', tool: 'repair_integration', callId: 'call-c' },
  { eventType: 'POLICY_DECISION', tool: 'repair_integration', callId: 'call-c', decision: 'NEEDS_APPROVAL', reasonCode: 'REPAIR_NEEDS_APPROVAL', ruleIds: ['repair.approval'] },
  { eventType: 'APPROVAL_REQUESTED', tool: 'repair_integration', callId: 'call-c' },
  { eventType: 'CALL_RECEIVED', tool: 'run_generated_tests', callId: 'call-d' },
  { eventType: 'POLICY_DECISION', tool: 'run_generated_tests', callId: 'call-d', decision: 'DENY', reasonCode: 'FLOOR_EXECUTE_NON_MOCK', ruleIds: ['floor.execute_mock_only'] },
  { eventType: 'CALL_RECEIVED', tool: 'propose_mapping', callId: 'call-e' },
  { eventType: 'POLICY_DECISION', tool: 'propose_mapping', callId: 'call-e', decision: 'NEEDS_APPROVAL', reasonCode: 'MODEL_CALL_ON_NON_SYNTHETIC_DATA', ruleIds: ['model.other'] },
  { eventType: 'APPROVAL_REQUESTED', tool: 'propose_mapping', callId: 'call-e' },
  { eventType: 'CALL_RECEIVED', tool: 'get_integration', callId: 'call-f' },
  { eventType: 'POLICY_DECISION', tool: 'get_integration', callId: 'call-f', decision: 'ALLOW', reasonCode: 'READ_ONLY', ruleIds: ['read.any'] },
  { eventType: 'OUTPUT_REDACTED', tool: 'get_integration', callId: 'call-f', reasonCode: 'SECRET_PATTERN' },
]

const GENESIS = '0'.repeat(64)

async function sha256(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('')
}

type Unhashed = Omit<AuditEvent, 'prevHash' | 'rowHash'>

function canonical(e: Unhashed): string {
  return JSON.stringify(Object.fromEntries(Object.entries(e).sort(([a], [b]) => a.localeCompare(b))))
}

async function chain(events: Unhashed[]): Promise<AuditEvent[]> {
  const out: AuditEvent[] = []
  let prev = GENESIS
  for (const e of events) {
    const rowHash = await sha256(prev + canonical(e))
    out.push({ ...e, prevHash: prev, rowHash })
    prev = rowHash
  }
  return out
}

/** Recomputes every row hash from its predecessor; the first mismatch breaks the chain. */
export async function verifyChain(events: AuditEvent[]): Promise<number | null> {
  let prev = GENESIS
  for (const e of events) {
    const { prevHash, rowHash, ...rest } = e
    if (prevHash !== prev || rowHash !== (await sha256(prev + canonical(rest)))) return e.seq
    prev = rowHash
  }
  return null
}

function unhashed(row: Row, seq: number): Unhashed {
  return {
    seq,
    eventType: row.eventType,
    session: 'fixture-session',
    principal: 'stdio:operator',
    tool: row.tool,
    callId: row.callId,
    decision: row.decision ?? null,
    reasonCode: row.reasonCode ?? null,
    ruleIds: row.ruleIds ?? [],
    policyHash: POLICY_HASH,
  }
}

/** An in-memory stand-in for the v0.6 API. A decision changes only this browser tab's state. */
export function createFixturePolicyApi(): PolicyApi {
  const log: Unhashed[] = ROWS.map((r, i) => unhashed(r, i + 1))
  const approvals: Approval[] = [
    {
      id: 'apr-preview-1',
      tool: 'repair_integration',
      principal: 'stdio:operator',
      requestHash: 'FIXTURE-request-c',
      policyHash: POLICY_HASH,
      status: 'PENDING',
      reasonCode: 'REPAIR_NEEDS_APPROVAL',
    },
    {
      id: 'apr-preview-2',
      tool: 'propose_mapping',
      principal: 'stdio:operator',
      requestHash: 'FIXTURE-request-e',
      policyHash: POLICY_HASH,
      status: 'PENDING',
      reasonCode: 'MODEL_CALL_ON_NON_SYNTHETIC_DATA',
    },
  ]
  return {
    isFixture: true,
    active: async () => POLICY,
    events: () => chain(log),
    verify: async () => {
      const events = await chain(log)
      const bad = await verifyChain(events)
      return { ok: bad === null, checked: events.length, firstBadSeq: bad }
    },
    approvals: async () => approvals.map((a) => ({ ...a })),
    decide: async (id, decision) => {
      const approval = approvals.find((a) => a.id === id)
      if (!approval) throw new Error(`no approval ${id}`)
      if (approval.status !== 'PENDING') throw new Error(`${id} is already ${approval.status}`)
      approval.status = decision
      const requested = log.find((e) => e.eventType === 'APPROVAL_REQUESTED' && e.tool === approval.tool)
      log.push(
        unhashed(
          { eventType: 'APPROVAL_DECIDED', tool: approval.tool, callId: requested?.callId ?? id, reasonCode: `HUMAN_${decision}` },
          log.length + 1,
        ),
      )
      return { ...approval }
    },
  }
}
