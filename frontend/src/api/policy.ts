/**
 * The one place policy, audit and approval data comes from.
 *
 * FIXTURE: the v0.6 endpoints (GET /policy/active, /audit/events, /audit/verify, /approvals and
 * POST /approvals/{id}/decide) do not exist yet, so this binds the in-memory fixture in both modes.
 * When they land, implement `PolicyApi` over `rest` in this file and change the export below; no
 * screen changes.
 */
import { createFixturePolicyApi } from '../fixtures/policy.fixture'
import type { PolicyApi } from './policyTypes'

export const policyApi: PolicyApi = createFixturePolicyApi()
