/** Policy, audit and approval shapes from docs/plans/v0.6-mcp-policy-plan.md (sections 3, 4 and 6).
 * The v0.6 endpoints do not exist yet; these types are what the UI expects them to return. */

export type Decision = 'ALLOW' | 'DENY' | 'NEEDS_APPROVAL'

export interface PolicyRule {
  id: string
  tools: string[]
  roles: string[]
  environments: string[]
  dataClasses: string[]
  outcome: 'allow' | 'deny' | 'needs_approval'
  reasonCode: string
}

export interface FloorRule {
  id: string
  description: string
}

export interface ActivePolicy {
  version: string
  policyHash: string
  file: string
  rules: PolicyRule[]
  floor: FloorRule[]
}

export type AuditEventType =
  | 'CALL_RECEIVED'
  | 'POLICY_DECISION'
  | 'APPROVAL_REQUESTED'
  | 'APPROVAL_DECIDED'
  | 'CALL_EXECUTED'
  | 'OUTPUT_REDACTED'

export interface AuditEvent {
  seq: number
  eventType: AuditEventType
  session: string
  principal: string
  tool: string
  callId: string
  decision: Decision | null
  reasonCode: string | null
  ruleIds: string[]
  policyHash: string
  prevHash: string
  rowHash: string
}

export interface ChainVerification {
  ok: boolean
  checked: number
  firstBadSeq: number | null
}

export type ApprovalStatus = 'PENDING' | 'APPROVED' | 'DENIED' | 'EXPIRED' | 'CONSUMED'

export interface Approval {
  id: string
  tool: string
  principal: string
  requestHash: string
  policyHash: string
  status: ApprovalStatus
  reasonCode: string
}

export interface PolicyApi {
  /** True while the implementation is a fixture rather than the v0.6 REST API. */
  readonly isFixture: boolean
  active(): Promise<ActivePolicy>
  events(): Promise<AuditEvent[]>
  verify(): Promise<ChainVerification>
  approvals(): Promise<Approval[]>
  decide(id: string, decision: 'APPROVED' | 'DENIED'): Promise<Approval>
}
