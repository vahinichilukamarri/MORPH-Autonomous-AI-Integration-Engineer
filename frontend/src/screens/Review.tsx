import { useState } from 'react'

import { MODE } from '../api/mode'
import { rest, type Mapping } from '../api/rest'
import { useAsync } from '../app/useAsync'
import { Badge, EmptyState, ErrorState, PageHead, Panel, Skeleton } from '../components/ui'
import type { Tone } from '../data/derive'
import { recorded } from '../data/recorded'

/** `field: REASON` as recorded in blocked_reasons. */
function parseReason(text: string): { field: string; reason: string } {
  const i = text.lastIndexOf(':')
  return i < 0 ? { field: text, reason: '' } : { field: text.slice(0, i).trim(), reason: text.slice(i + 1).trim() }
}

/** backend/app/mapping/confidence.py ReviewStatus. */
const REVIEW_TONE: Record<string, Tone> = {
  AUTO_ACCEPTED: 'ok',
  APPROVED: 'ok',
  OVERRIDDEN: 'accent',
  NEEDS_REVIEW: 'human',
}

function LiveMappings() {
  const [draft, setDraft] = useState('')
  const [runId, setRunId] = useState<number | null>(null)
  return (
    <Panel title="Live: a mapping run from the API" labelledBy="live-review">
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault()
          const id = Number(draft)
          setRunId(Number.isInteger(id) && id > 0 ? id : null)
        }}
      >
        <label htmlFor="run-id">Mapping run id</label>
        <input id="run-id" className="input" inputMode="numeric" value={draft} onChange={(e) => setDraft(e.target.value)} />
        <button type="submit" className="button">
          Load
        </button>
      </form>
      {runId === null ? (
        <EmptyState title="No run selected">Enter the id returned by POST /mapping-runs.</EmptyState>
      ) : (
        <LiveTable runId={runId} />
      )}
    </Panel>
  )
}

function LiveTable({ runId }: { runId: number }) {
  const [state, reload] = useAsync((signal) => rest.mappings(runId, signal), [runId])
  if (state.kind === 'loading') return <Skeleton lines={4} />
  if (state.kind === 'error') return <ErrorState error={state.error} onRetry={reload} />
  if (state.data.length === 0) return <EmptyState title="This run has no mappings" />
  return (
    <table className="table table--compact">
      <thead>
        <tr>
          <th scope="col">Target field</th>
          <th scope="col">Type</th>
          <th scope="col">Source fields</th>
          <th scope="col">Validation</th>
          <th scope="col">Review</th>
          <th scope="col">Confidence</th>
        </tr>
      </thead>
      <tbody>
        {state.data.map((m: Mapping) => (
          <tr key={m.id}>
            <td className="mono">{m.target_field}</td>
            <td className="mono small">{m.current.mapping_type}</td>
            <td className="mono small">{m.current.source_fields.join(', ') || '-'}</td>
            <td className="mono small">{m.current.validation_status}</td>
            <td>
              <Badge tone={REVIEW_TONE[m.current.review_status] ?? 'skipped'}>{m.current.review_status}</Badge>
            </td>
            <td className="num mono">{m.current.confidence === null ? '-' : m.current.confidence.toFixed(2)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function Review() {
  const data = recorded
  const blocked = data.units.filter((u) => u.inputSet === 'as_proposed' && u.condition === 'D')
  const s1Blocked = new Map(
    (blocked.find((u) => u.scenario === data.review.scenario)?.blockedReasons ?? []).map((r) => {
      const { field, reason } = parseReason(r)
      return [field, reason]
    }),
  )
  const scenario = data.scenarios.find((s) => s.id === data.review.scenario)

  return (
    <div className="page">
      <PageHead
        title="Review gate"
        lede={
          <>
            Only fields a person or the confidence policy has accepted are compiled. If a required, writable target field
            is not accepted, the integration stops at <code>BLOCKED_PENDING_REVIEW</code>: no code is generated, no model is
            called, and nothing partial ships silently.
          </>
        }
      />

      <div className="gate-explainer" aria-label="How the gate decides">
        <div className="gate-explainer__step">
          <span className="mono faint">input</span>
          <span>Mapping proposals with validation status and confidence</span>
        </div>
        <div className="gate-explainer__arrow" aria-hidden="true">
          →
        </div>
        <div className="gate-explainer__step">
          <span className="mono faint">rule</span>
          <span>
            A required field that is <code>NEEDS_REVIEW</code> or <code>UNRESOLVED</code> blocks
          </span>
        </div>
        <div className="gate-explainer__arrow" aria-hidden="true">
          →
        </div>
        <div className="gate-explainer__step gate-explainer__step--blocked">
          <span className="mono faint">result</span>
          <Badge tone="blocked">BLOCKED_PENDING_REVIEW</Badge>
        </div>
      </div>

      <Panel title="Recorded: the as-proposed inputs, per scenario" labelledBy="blocked-scenarios">
        <ul className="blocked-list">
          {blocked.map((u) => {
            const s = data.scenarios.find((x) => x.id === u.scenario)
            return (
              <li key={u.key} className="blocked-item">
                <div className="blocked-item__head">
                  <span className="mono strong">{s?.label}</span>
                  <span className="muted">
                    {s?.source} → {s?.target}
                  </span>
                  <Badge tone="blocked">{u.status}</Badge>
                </div>
                <ul className="field-pills">
                  {u.blockedReasons.map((r) => {
                    const { field, reason } = parseReason(r)
                    return (
                      <li key={r} className={`field-pill field-pill--${reason === 'UNRESOLVED' ? 'blocked' : 'human'}`}>
                        <span className="mono">{field}</span>
                        <span className="field-pill__reason">{reason}</span>
                      </li>
                    )
                  })}
                </ul>
              </li>
            )
          })}
        </ul>
        <p className="footnote">
          The same blocks apply to D, L1 and L2: the gate runs before codegen, so every condition stops here with zero
          model calls.
        </p>
      </Panel>

      <Panel title={`Recorded: the ${scenario?.label ?? ''} proposals (${data.review.mode} replay)`} labelledBy="s1-proposals">
        <div className="table-scroll" tabIndex={0} role="region" aria-labelledby="s1-proposals">
          <table className="table table--compact">
            <thead>
              <tr>
                <th scope="col">Target field</th>
                <th scope="col">Proposed by {data.review.model}</th>
                <th scope="col">Review gate</th>
                <th scope="col">Answer key (graded afterwards)</th>
              </tr>
            </thead>
            <tbody>
              {data.review.fields.map((f) => {
                const reason = s1Blocked.get(f.targetField)
                return (
                  <tr key={f.targetField}>
                    <td className="mono">{f.targetField}</td>
                    <td className="mono small">{f.proposed}</td>
                    <td>
                      {reason ? (
                        <Badge tone="human">blocks · {reason}</Badge>
                      ) : (
                        <span className="faint">not blocking (status not recorded)</span>
                      )}
                    </td>
                    <td>{f.matchesAnswerKey ? <Badge tone="ok">matches</Badge> : <Badge tone="fail">differs</Badge>}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <p className="footnote">
          A proposal can match the answer key and still be held for review: the gate acts on the pipeline&apos;s own
          confidence, never on the answer key, which only the bench grader reads.
        </p>
      </Panel>

      {MODE === 'live' && <LiveMappings />}
    </div>
  )
}
