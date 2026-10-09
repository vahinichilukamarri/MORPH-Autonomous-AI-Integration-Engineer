import { policyApi } from '../api/policy'
import type { Decision } from '../api/policyTypes'
import { href } from '../app/router'
import { useAsync } from '../app/useAsync'
import { Icon } from '../components/Icon'
import { Badge, ErrorState, Skeleton } from '../components/ui'
import type { Tone } from '../data/derive'

const DECISION_TONE: Record<Decision, Tone> = { ALLOW: 'ok', DENY: 'fail', NEEDS_APPROVAL: 'human' }

/** A small window onto the Policy and audit screen. The data comes from the one policy adapter,
 * which serves FIXTURE data until the v0.6 endpoints exist; the UI calls it Preview. */
export function PolicyPreview() {
  const [state, reload] = useAsync(
    () => Promise.all([policyApi.events(), policyApi.verify(), policyApi.approvals()]),
    [],
  )
  if (state.kind === 'loading') return <Skeleton lines={5} label="Loading the policy preview" />
  if (state.kind === 'error') return <ErrorState error={state.error} onRetry={reload} />
  const [events, verification, approvals] = state.data
  const decisions = events.filter((e) => e.decision !== null).slice(0, 5)
  const pending = approvals.filter((a) => a.status === 'PENDING').length
  return (
    <div className="ppreview">
      <div className="ppreview__bar">
        <span className="preview-tag">Preview</span>
        <span className="mono small faint">illustrative data · not a recorded run</span>
        <span className="ppreview__spacer" />
        {verification.ok ? (
          <Badge tone="ok" title="Recomputed in your browser over the preview rows">
            hash chain verified
          </Badge>
        ) : (
          <Badge tone="fail">chain broken</Badge>
        )}
      </div>
      <ul className="ppreview__log" aria-label="Decision log, preview">
        {decisions.map((e) => (
          <li key={e.seq}>
            <span className="mono faint">#{e.seq}</span>
            <code>{e.tool}</code>
            {e.decision && <Badge tone={DECISION_TONE[e.decision]}>{e.decision}</Badge>}
            <span className="mono small faint ppreview__reason">{e.reasonCode}</span>
            <span className="mono small faint ppreview__hash">{e.rowHash.slice(0, 10)}…</span>
          </li>
        ))}
      </ul>
      <div className="ppreview__foot">
        <span className="small muted">
          {pending > 0 ? `${pending} approval${pending === 1 ? '' : 's'} waiting in the preview queue` : 'No approvals waiting'}
        </span>
        <a href={href('policy')} className="btn btn--ghost btn--sm">
          Open the policy screen <Icon name="arrowRight" size={16} />
        </a>
      </div>
    </div>
  )
}
