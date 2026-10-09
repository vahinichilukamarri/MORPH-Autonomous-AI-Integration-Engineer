import { useState } from 'react'

import { policyApi } from '../api/policy'
import type { ActivePolicy, Approval, AuditEvent, ChainVerification, Decision } from '../api/policyTypes'
import { useAsync } from '../app/useAsync'
import { Tabs } from '../components/Tabs'
import { useToast } from '../components/Toast'
import { Badge, Card, EmptyState, ErrorState, PageHead, PreviewBanner, PreviewTag, Skeleton } from '../components/ui'
import type { Tone } from '../data/derive'

const DECISION_TONE: Record<Decision, Tone> = { ALLOW: 'ok', DENY: 'fail', NEEDS_APPROVAL: 'human' }
const OUTCOME_TONE: Record<string, Tone> = { allow: 'ok', deny: 'fail', needs_approval: 'human' }
const APPROVAL_TONE: Record<string, Tone> = {
  PENDING: 'human',
  APPROVED: 'ok',
  DENIED: 'fail',
  EXPIRED: 'skipped',
  CONSUMED: 'skipped',
}

interface Snapshot {
  policy: ActivePolicy
  events: AuditEvent[]
  verification: ChainVerification
  approvals: Approval[]
}

async function load(): Promise<Snapshot> {
  const [policy, events, verification, approvals] = await Promise.all([
    policyApi.active(),
    policyApi.events(),
    policyApi.verify(),
    policyApi.approvals(),
  ])
  return { policy, events, verification, approvals }
}

function ChainBadge({ v }: { v: ChainVerification }) {
  return v.ok ? (
    <Badge tone="ok" title="Every row hash recomputed from its predecessor">
      Hash chain verified · {v.checked} rows
    </Badge>
  ) : (
    <Badge tone="fail">Chain broken at row {v.firstBadSeq}</Badge>
  )
}

export default function Policy() {
  const [state, reload] = useAsync(() => load(), [])
  const [busy, setBusy] = useState<string | null>(null)
  const [decideError, setDecideError] = useState<Error | null>(null)
  const [tab, setTab] = useState<'log' | 'rules'>('log')
  const notify = useToast()

  const decide = async (id: string, decision: 'APPROVED' | 'DENIED') => {
    setBusy(id)
    setDecideError(null)
    try {
      const approval = await policyApi.decide(id, decision)
      notify(
        <>
          <code>{approval.id}</code> {decision === 'APPROVED' ? 'approved' : 'denied'} (preview data, this tab only)
        </>,
        decision === 'APPROVED' ? 'ok' : 'fail',
      )
      reload()
    } catch (e) {
      setDecideError(e instanceof Error ? e : new Error(String(e)))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="page">
      <PageHead
        eyebrow={<>Policy &amp; audit <PreviewTag /></>}
        title="Policy & audit"
        lede="The v0.6 policy layer, in progress, is designed to gate every MCP tool call: deterministic rules, a floor the policy file cannot override, an append-only hash-chained audit log, and human approvals bound to the request and the policy."
      />
      {policyApi.isFixture && (
        <PreviewBanner>
          v0.6 is in progress and its endpoints are not wired to this UI yet. Everything on this screen is illustrative
          data in the planned shapes, not a recorded run. The hash chain is computed in your browser over these rows;
          approving or denying changes only this tab.
        </PreviewBanner>
      )}
      {state.kind === 'loading' && <Skeleton lines={8} label="Loading policy" />}
      {state.kind === 'error' && <ErrorState error={state.error} onRetry={reload} />}
      {state.kind === 'ok' && (
        <>
          <div className="two-col">
            <Card
              title="Active policy"
              labelledBy="active-policy"
              actions={<span className="mono small faint">{state.data.policy.version}</span>}
            >
              <dl className="kv">
                <div>
                  <dt>File</dt>
                  <dd className="mono small">{state.data.policy.file}</dd>
                </div>
                <div>
                  <dt>Policy hash</dt>
                  <dd className="mono small">{state.data.policy.policyHash}</dd>
                </div>
                <div>
                  <dt>Evaluation</dt>
                  <dd>Any deny wins, then needs-approval, then allow; otherwise deny.</dd>
                </div>
              </dl>
              <h3 className="subhead">Floor (in code, cannot be overridden)</h3>
              <ul className="floor">
                {state.data.policy.floor.map((f) => (
                  <li key={f.id}>
                    <code>{f.id}</code> {f.description}
                  </li>
                ))}
              </ul>
            </Card>
            <Card title="Approval queue" labelledBy="approvals">
              {decideError && <ErrorState error={decideError} />}
              {state.data.approvals.length === 0 ? (
                <EmptyState title="Nothing waiting" />
              ) : (
                <ul className="approvals">
                  {state.data.approvals.map((a) => (
                    <li key={a.id} className="approval">
                      <div className="approval__head">
                        <code>{a.tool}</code>
                        <Badge tone={APPROVAL_TONE[a.status] ?? 'skipped'}>{a.status}</Badge>
                      </div>
                      <div className="approval__meta mono small faint">
                        {a.id} · {a.principal} · {a.reasonCode}
                      </div>
                      {a.status === 'PENDING' && (
                        <div className="approval__actions">
                          <button
                            type="button"
                            className="btn btn--secondary"
                            disabled={busy === a.id}
                            onClick={() => void decide(a.id, 'APPROVED')}
                          >
                            Approve
                          </button>
                          <button
                            type="button"
                            className="btn btn--danger"
                            disabled={busy === a.id}
                            onClick={() => void decide(a.id, 'DENIED')}
                          >
                            Deny
                          </button>
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </div>

          <Card title="Decision log and rules" labelledBy="audit" actions={<ChainBadge v={state.data.verification} />}>
            <Tabs
              label="Policy views"
              value={tab}
              onChange={setTab}
              tabs={[
                { id: 'log', label: 'Decision log' },
                { id: 'rules', label: 'Rules' },
              ]}
              panel={
                tab === 'log' ? (
                  <div className="table-scroll" >
              <table className="table table--compact audit">
                <thead>
                  <tr>
                    <th scope="col">#</th>
                    <th scope="col">Event</th>
                    <th scope="col">Tool</th>
                    <th scope="col">Call</th>
                    <th scope="col">Decision</th>
                    <th scope="col">Reason</th>
                    <th scope="col">Row hash</th>
                  </tr>
                </thead>
                <tbody>
                  {state.data.events.map((e) => (
                    <tr key={e.seq}>
                      <td className="num mono">{e.seq}</td>
                      <td className="mono small">{e.eventType}</td>
                      <td className="mono small">{e.tool}</td>
                      <td className="mono small faint">{e.callId}</td>
                      <td>{e.decision ? <Badge tone={DECISION_TONE[e.decision]}>{e.decision}</Badge> : <span className="faint">-</span>}</td>
                      <td className="mono small">{e.reasonCode ?? ''}</td>
                      <td className="mono small faint" title={`prev ${e.prevHash}`}>
                        {e.rowHash.slice(0, 12)}…
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
                ) : (
                  <div className="table-scroll" >
              <table className="table table--compact">
                <thead>
                  <tr>
                    <th scope="col">Rule</th>
                    <th scope="col">Tools</th>
                    <th scope="col">Roles</th>
                    <th scope="col">Environment</th>
                    <th scope="col">Data class</th>
                    <th scope="col">Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {state.data.policy.rules.map((r) => (
                    <tr key={r.id}>
                      <td className="mono">{r.id}</td>
                      <td className="mono small">{r.tools.join(', ')}</td>
                      <td className="small">{r.roles.join(', ')}</td>
                      <td className="small">{r.environments.join(', ')}</td>
                      <td className="small">{r.dataClasses.join(', ')}</td>
                      <td>
                        <Badge tone={OUTCOME_TONE[r.outcome] ?? 'skipped'}>{r.outcome}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
                )
              }
            />
          </Card>
        </>
      )}
    </div>
  )
}
