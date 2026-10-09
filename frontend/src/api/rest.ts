/** Typed client for the existing REST API (live mode). Shapes mirror backend/app/api/*.py. */

export interface Health {
  status: string
  database: string
}

export interface MappingRun {
  id: number
  source_version_id: number
  target_version_id: number
  source_entity: string
  target_entity: string
  mode: string
  provider: string
  model: string
  prompt_version: string
  confidence_version: string
  temperature: number
  requirement: string | null
  summary: Record<string, unknown>
  run_reasons: unknown[]
  created_at: string
}

export interface MappingVersion {
  version: number
  author: string
  mapping_type: string
  source_fields: string[]
  transformation: Record<string, unknown> | null
  unresolved_reason: string | null
  rationale: string
  alternatives: unknown[]
  certainty: string
  validation_status: string
  validation_reasons: unknown[]
  outputs_preview: unknown[]
  confidence: number | null
  review_status: string
  review_reasons: string[]
  created_at: string
}

export interface Mapping {
  id: number
  run_id: number
  target_field: string
  versions: number
  current: MappingVersion
}

export interface Exclusion {
  target_field: string
  reason: string
  required: boolean
  detail: string
}

export interface ReviewDecision {
  gate_status: string
  included: string[]
  excluded: Exclusion[]
  not_writable: string[]
  blocking: Exclusion[]
}

export interface Integration {
  id: number
  mapping_run_id: number
  condition: string
  name: string
  versions: number[]
}

export interface IntegrationVersion {
  id: number
  integration_id: number
  version: number
  status: string
  bundle_hash: string | null
  input_hash: string
  generator_version: string
  runtime_version: string
  mapping_version_ids: unknown[]
  review: ReviewDecision | null
  plan_error: string | null
  created_at: string
}

export interface GateResult {
  stage: string
  passed: boolean
  findings: unknown[]
}

export interface SandboxRun {
  id: number
  purpose: string
  outcome: string
  exit_code: number | null
  duration_s: number
  result: Record<string, unknown> | null
}

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

const BASE = import.meta.env.VITE_MORPH_API_BASE ?? '/api'

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${BASE}${path}`, { signal, headers: { Accept: 'application/json' } })
  if (!response.ok) throw new ApiError(response.status, `${response.status} ${response.statusText} for ${path}`)
  return (await response.json()) as T
}

export const rest = {
  health: (signal?: AbortSignal) => get<Health>('/health', signal),
  mappingRun: (id: number, signal?: AbortSignal) => get<MappingRun>(`/mapping-runs/${id}`, signal),
  mappings: (runId: number, signal?: AbortSignal) => get<Mapping[]>(`/mapping-runs/${runId}/mappings`, signal),
  integration: (id: number, signal?: AbortSignal) => get<Integration>(`/integrations/${id}`, signal),
  version: (id: number, n: number, signal?: AbortSignal) =>
    get<IntegrationVersion>(`/integrations/${id}/versions/${n}`, signal),
  gate: (id: number, n: number, signal?: AbortSignal) =>
    get<GateResult[]>(`/integrations/${id}/versions/${n}/gate`, signal),
  sandboxRuns: (id: number, n: number, signal?: AbortSignal) =>
    get<SandboxRun[]>(`/integrations/${id}/versions/${n}/sandbox-runs`, signal),
}
