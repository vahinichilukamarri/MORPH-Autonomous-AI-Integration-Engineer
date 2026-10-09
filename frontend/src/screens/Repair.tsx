import { lazy, type RefObject, Suspense, useEffect, useMemo, useRef, useState } from 'react'

import { navigate } from '../app/router'
import { useAppliedTheme } from '../app/theme'
import { useAsync } from '../app/useAsync'
import { Badge, Chip, EmptyState, ErrorState, PageHead, Panel, Segmented, Skeleton } from '../components/ui'
import {
  EXPOSURE_INFO,
  fmt,
  OUTCOME_LABEL,
  OUTCOME_TONE,
  oracleSum,
  outcomeOf,
  repairsUsed,
  scenarioLabel,
} from '../data/derive'
import { loadReplies, recorded } from '../data/recorded'
import type { Attempt, Unit } from '../data/types'

const DiffView = lazy(() => import('./DiffView'))

const REPAIR_UNITS = recorded.units.filter((u) => u.milestone === 'v0.5')

const codes = (a: Attempt | undefined) => new Set((a?.feedback ?? []).map((f) => `${f.stage}.${f.code}`))

function Transition({ prev, next }: { prev: Attempt; next: Attempt }) {
  const before = codes(prev)
  const after = codes(next)
  const cleared = [...before].filter((c) => !after.has(c))
  const kept = [...before].filter((c) => after.has(c))
  const added = [...after].filter((c) => !before.has(c))
  return (
    <dl className="transition">
      <div>
        <dt>Cleared</dt>
        <dd>{cleared.length ? cleared.map((c) => <Chip key={c}>{c}</Chip>) : <span className="faint">none</span>}</dd>
      </div>
      <div>
        <dt>Still present</dt>
        <dd>{kept.length ? kept.map((c) => <Chip key={c}>{c}</Chip>) : <span className="faint">none</span>}</dd>
      </div>
      <div>
        <dt>New</dt>
        <dd>{added.length ? added.map((c) => <Chip key={c}>{c}</Chip>) : <span className="faint">none</span>}</dd>
      </div>
    </dl>
  )
}

function AttemptDetail({ unit, attempt }: { unit: Unit; attempt: Attempt }) {
  const prev = unit.attempts.find((a) => a.n === attempt.n - 1)
  return (
    <div className="attempt-detail">
      <dl className="kv kv--row">
        <div>
          <dt>Source</dt>
          <dd>{attempt.source === 'network' ? 'Repair call' : 'Seeded: recorded v0.4 reply, no new call'}</dd>
        </div>
        <div>
          <dt>Result</dt>
          <dd>
            {attempt.failedStage ? (
              <Badge tone="fail">failed at {attempt.failedStage}</Badge>
            ) : (
              <Badge tone="ok">passed every stage</Badge>
            )}
          </dd>
        </div>
        <div>
          <dt>Tokens</dt>
          <dd className="mono small">
            {fmt(attempt.usage.input)} in · {fmt(attempt.usage.output)} out · {fmt(attempt.usage.reasoning)} reasoning
          </dd>
        </div>
        <div>
          <dt>Latency</dt>
          <dd className="mono small">{fmt(attempt.usage.latencyMs)} ms</dd>
        </div>
        <div>
          <dt>Finish reason</dt>
          <dd className="mono small">{attempt.finishReason ?? 'not recorded'}</dd>
        </div>
      </dl>

      <h3 className="subhead">Structured feedback{attempt.failedStage ? ' sent to the next attempt' : ''}</h3>
      {attempt.feedback.length ? (
        <table className="table table--compact">
          <thead>
            <tr>
              <th scope="col">Stage</th>
              <th scope="col">Code</th>
              <th scope="col">File</th>
              <th scope="col">Line</th>
            </tr>
          </thead>
          <tbody>
            {attempt.feedback.map((f, i) => (
              <tr key={i}>
                <td className="mono">{f.stage}</td>
                <td className="mono">{f.code}</td>
                <td className="faint small">not recorded</td>
                <td className="faint small">not recorded</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <EmptyState title="No feedback">This attempt passed every stage, so nothing was fed back.</EmptyState>
      )}
      {attempt.feedback.length > 0 && (
        <p className="footnote">
          The committed results keep each item as stage and code. Its message, file and line went into the repair prompt,
          which is stored only as a hash, so they are shown as not recorded rather than reconstructed.
        </p>
      )}

      <h3 className="subhead">Guards</h3>
      <div className="guards">
        {attempt.guardEnforced.length ? (
          attempt.guardEnforced.map((g, i) => (
            <Badge key={`e${i}`} tone="fail" title="Enforced guard: the attempt was rejected">
              {g} tripped
            </Badge>
          ))
        ) : (
          <span className="faint small">No enforced guard tripped.</span>
        )}
        {attempt.guardShadow.map((g, i) => (
          <Badge key={`s${i}`} tone="skipped" title="Shadow guard: reported, never rejects">
            {g} shadow
          </Badge>
        ))}
      </div>

      {prev && (
        <>
          <h3 className="subhead">
            Attempt {prev.n} to {attempt.n}
          </h3>
          <Transition prev={prev} next={attempt} />
        </>
      )}
    </div>
  )
}

/** True once the element has come within a screen of the viewport; stays true. */
function useNearViewport<T extends Element>(): [RefObject<T | null>, boolean] {
  const ref = useRef<T>(null)
  const [near, setNear] = useState(typeof IntersectionObserver === 'undefined')
  useEffect(() => {
    const el = ref.current
    if (near || !el) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) setNear(true)
      },
      { rootMargin: '200px 0px' },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [near])
  return [ref, near]
}

/** The editor and the replies are the heaviest part of the app, so they load only when the diff
 * panel is about to be seen. */
function Diff({ unit, attempt }: { unit: Unit; attempt: Attempt }) {
  const [ref, near] = useNearViewport<HTMLDivElement>()
  return <div ref={ref}>{near ? <DiffLoaded unit={unit} attempt={attempt} /> : <Skeleton lines={10} label="Waiting to load the diff" />}</div>
}

function DiffLoaded({ unit, attempt }: { unit: Unit; attempt: Attempt }) {
  const [replies, reload] = useAsync(() => loadReplies(), [])
  const theme = useAppliedTheme()
  if (replies.kind === 'loading') return <Skeleton lines={10} label="Loading the recorded replies" />
  if (replies.kind === 'error') return <ErrorState error={replies.error} onRetry={reload} />
  const prev = unit.attempts.find((a) => a.n === attempt.n - 1)
  const now = replies.data.replies[attempt.replyId]
  const before = prev ? replies.data.replies[prev.replyId] : undefined
  if (!now) return <EmptyState title="Reply not in the committed replays" />
  return (
    <>
      <p className="diff-caption small">
        {prev ? (
          <>
            Attempt {prev.n} (left) to attempt {attempt.n} (right): the model&apos;s {now.kind === 'module' ? 'sync module' : 'strategy'}{' '}
            as recorded.
          </>
        ) : (
          <>Attempt 0, the recorded v0.4 reply that every repair started from.</>
        )}
        {now.notes && <span className="faint"> Model notes: {now.notes}</span>}
      </p>
      <Suspense fallback={<Skeleton lines={10} label="Loading the editor" />}>
        <DiffView
          original={before?.text ?? ''}
          modified={now.text}
          language={now.kind === 'module' ? 'python' : 'javascript'}
          theme={theme}
          label={`Diff of attempt ${attempt.n}`}
        />
      </Suspense>
    </>
  )
}

function RepairUnit({ unit }: { unit: Unit }) {
  const [selected, setSelected] = useState(unit.attempts[unit.attempts.length - 1]?.n ?? 0)
  const attempt = unit.attempts.find((a) => a.n === selected) ?? unit.attempts[0]
  const outcome = outcomeOf(unit)
  const sum = oracleSum(unit)
  const max = recorded.runs.find((r) => r.milestone === 'v0.5')?.maxRepairAttempts
  const timeline = useMemo(() => unit.attempts, [unit])

  return (
    <>
      <div className={`terminal terminal--${OUTCOME_TONE[outcome]}`} role="status">
        <div className="terminal__main">
          <span className="terminal__label">Terminal state</span>
          <span className="terminal__value mono">{unit.status}</span>
          {unit.reason && <span className="terminal__reason mono">{unit.reason}</span>}
        </div>
        <dl className="terminal__facts">
          <div>
            <dt>Repairs used</dt>
            <dd className="mono">
              {repairsUsed(unit)} of {max ?? '?'}
            </dd>
          </div>
          <div>
            <dt>Oracle</dt>
            <dd className="mono">{sum ? `${sum.passed}/${sum.total} checks` : 'not graded'}</dd>
          </div>
          <div>
            <dt>Verdict</dt>
            <dd>
              <Badge tone={OUTCOME_TONE[outcome]}>{OUTCOME_LABEL[outcome]}</Badge>
            </dd>
          </div>
          {unit.exposure && (
            <div>
              <dt>Exposure</dt>
              <dd>
                <Chip title={EXPOSURE_INFO[unit.exposure]}>{unit.exposure}</Chip>
              </dd>
            </div>
          )}
        </dl>
      </div>

      <ol className="timeline" aria-label="Attempts">
        {timeline.map((a) => (
          <li key={a.n} className="timeline__step">
            <button
              type="button"
              className={`timeline__node timeline__node--${a.failedStage ? 'fail' : 'ok'}`}
              aria-pressed={a.n === selected}
              aria-label={`Attempt ${a.n}: ${a.failedStage ? `failed at ${a.failedStage}` : 'READY'}, ${
                a.source === 'network' ? 'repair call' : 'seeded v0.4 reply'
              }${a.guardEnforced.length ? `, guard ${[...new Set(a.guardEnforced)].join(', ')} tripped` : ''}`}
              onClick={() => setSelected(a.n)}
            >
              <span className="timeline__n mono">Attempt {a.n}</span>
              <span className="timeline__stage mono">{a.failedStage ?? 'READY'}</span>
              <span className="timeline__src">{a.source === 'network' ? 'repair call' : 'seeded v0.4'}</span>
              {a.guardEnforced.length > 0 && <span className="timeline__guard">guard {[...new Set(a.guardEnforced)].join(', ')}</span>}
            </button>
          </li>
        ))}
        {unit.status === 'HUMAN_REVIEW_REQUIRED' && (
          <li className="timeline__step timeline__step--end">
            <span className="timeline__end mono">handed to a person</span>
          </li>
        )}
      </ol>

      <div className="repair-grid">
        <Panel title={`Attempt ${attempt.n}`} labelledBy="attempt-detail">
          <AttemptDetail unit={unit} attempt={attempt} />
        </Panel>
        <Panel title="Reply diff" labelledBy="reply-diff" className="repair-grid__diff">
          <Diff unit={unit} attempt={attempt} />
        </Panel>
      </div>
    </>
  )
}

export default function Repair({ params }: { params: URLSearchParams }) {
  const unit = REPAIR_UNITS.find((u) => u.key === params.get('unit')) ?? REPAIR_UNITS[0]
  if (!unit) {
    return (
      <div className="page">
        <EmptyState title="No repair runs in the recorded data" />
      </div>
    )
  }
  return (
    <div className="page">
      <PageHead
        title="Repair attempts"
        lede={`Fixed start: attempt 0 is the recorded v0.4 reply, then up to ${
          recorded.runs.find((r) => r.milestone === 'v0.5')?.maxRepairAttempts ?? '?'
        } repairs, each driven only by structured feedback from the validator, guards, static gate, generated tests and smoke test. The oracle grades once, after the loop, and never feeds back.`}
      />
      <Segmented
        label="Repair unit"
        value={unit.key}
        onChange={(key) => navigate('repair', { unit: key })}
        options={REPAIR_UNITS.map((u) => ({
          value: u.key,
          tone: OUTCOME_TONE[outcomeOf(u)],
          label: (
            <>
              <span className="mono">
                {scenarioLabel(recorded, u.scenario)} {u.condition}
              </span>
              <span className="seg__sub">{u.status === 'READY' ? (u.integrationCorrect ? 'correct' : 'incorrect') : 'human review'}</span>
            </>
          ),
        }))}
      />
      <RepairUnit key={unit.key} unit={unit} />
    </div>
  )
}
