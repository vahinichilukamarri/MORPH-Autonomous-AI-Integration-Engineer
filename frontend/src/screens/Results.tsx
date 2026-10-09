import * as m from 'motion/react-m'

import { href } from '../app/router'
import { useRun } from '../app/runs'
import { Icon } from '../components/Icon'
import { Tooltip } from '../components/Tooltip'
import { Badge, Card, Chip, PageHead } from '../components/ui'
import {
  CONDITION_INFO,
  CONDITIONS,
  EXPOSURE_INFO,
  findUnit,
  fmt,
  OUTCOME_LABEL,
  OUTCOME_TONE,
  oracleSum,
  outcomeOf,
  repairsUsed,
  runsPerUnit,
  summarise,
  totalsCheck,
} from '../data/derive'
import { recorded } from '../data/recorded'
import type { Condition, Milestone, Unit, Usage } from '../data/types'
import { CountUp } from '../motion/CountUp'
import { DURATION, EASE, STAGGER } from '../motion/tokens'

const RUN_OF: Record<Condition, Milestone> = { D: 'v0.4', L1: 'v0.4', L2: 'v0.4', L1R: 'v0.5', L2R: 'v0.5' }

function Tokens({ usage }: { usage: Usage | null }) {
  if (!usage) return <span className="faint">no model call</span>
  return (
    <span className="tokens mono">
      <span>
        {fmt(usage.input)} in · {fmt(usage.output)} out
      </span>
      <span className="faint">{fmt(usage.reasoning)} reasoning</span>
    </span>
  )
}

function Cell({ unit, current }: { unit: Unit | undefined; current: boolean }) {
  if (!unit) return <td className="cell cell--none">not run</td>
  const outcome = outcomeOf(unit)
  const sum = oracleSum(unit)
  const repairs = repairsUsed(unit)
  return (
    <td className={`cell cell--${OUTCOME_TONE[outcome]}${current ? ' is-current' : ''}`}>
      <div className="cell__top">
        <Badge tone={OUTCOME_TONE[outcome]}>{OUTCOME_LABEL[outcome]}</Badge>
        {outcome === 'READY_INCORRECT' && (
          <Tooltip content="Passed every gate and test MORPH controls, and still failed the hidden oracle.">
            <span className="cell__warn" tabIndex={0} role="img" aria-label="READY but incorrect">
              <Icon name="alert" size={16} />
            </span>
          </Tooltip>
        )}
      </div>
      <dl className="cell__facts">
        <div>
          <dt>Oracle</dt>
          <dd className="mono">{sum ? `${sum.passed}/${sum.total}` : 'not graded'}</dd>
        </div>
        {repairs !== null && (
          <div>
            <dt>{unit.status === 'READY' ? 'Repairs to READY' : 'Repairs used'}</dt>
            <dd className="mono">{repairs}</dd>
          </div>
        )}
        <div className="cell__tokens">
          <dt>
            {unit.milestone === 'v0.5' ? 'Repair tokens' : 'Tokens'}
            {unit.usage && ` (${unit.usage.calls} call${unit.usage.calls === 1 ? '' : 's'})`}
          </dt>
          <dd>
            <Tokens usage={unit.usage} />
          </dd>
        </div>
      </dl>
      {unit.exposure && (
        <Chip title={EXPOSURE_INFO[unit.exposure]}>
          <span className="faint">exposure</span> {unit.exposure}
        </Chip>
      )}
      <div className="cell__links">
        <a href={href('pipeline', { unit: unit.key })}>pipeline</a>
        {unit.milestone === 'v0.5' && <a href={href('repair', { unit: unit.key })}>attempts</a>}
      </div>
    </td>
  )
}

export default function Results() {
  const data = recorded
  const run = useRun()
  const n = runsPerUnit(data)
  const summaries = CONDITIONS.map((c) => summarise(data, c))
  const asProposed = data.units.filter((u) => u.inputSet === 'as_proposed')
  const totals = totalsCheck(data)
  const exposures = [...new Set(data.units.map((u) => u.exposure).filter((e): e is string => e !== null))]

  return (
    <div className="page">
      <PageHead
        eyebrow="Results"
        title="Results"
        lede={
          <>
            Each condition on the same {data.scenarios.length} scenarios with human-approved mappings. v0.4 ran once
            per unit; v0.5 started from the recorded v0.4 reply and allowed up to{' '}
            {data.runs.find((r) => r.milestone === 'v0.5')?.maxRepairAttempts ?? '?'} repairs.
          </>
        }
        aside={
          <div className="note note--warn" role="note">
            <strong>N={n} per unit.</strong> Nothing here is statistically significant; a one-unit difference is
            noise. READY is not correctness; only the oracle decides that.
          </div>
        }
      />

      <div className="summary-grid">
        {summaries.map((s, i) => (
          <m.article
            key={s.condition}
            className={`summary${RUN_OF[s.condition] === run ? ' is-current' : ''}${s.readyIncorrect > 0 ? ' summary--incorrect' : ''}`}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: DURATION.slow, ease: EASE.out, delay: i * STAGGER }}
          >
            <header>
              <span className="summary__cond mono">{s.condition}</span>
              <span className="summary__name">{CONDITION_INFO[s.condition].name}</span>
            </header>
            <div className="summary__big">
              <CountUp value={s.ready} />
              <span className="faint">/{s.units}</span> <span className="summary__unit">READY</span>
            </div>
            <div className="summary__run mono">{RUN_OF[s.condition]}</div>
            <ul className="summary__facts">
              <li>
                <span className="tone-ok">{s.correct}</span> oracle-correct
              </li>
              {s.readyIncorrect > 0 && (
                <li>
                  <span className="tone-incorrect">{s.readyIncorrect}</span> READY but incorrect
                </li>
              )}
              {s.humanReview > 0 && (
                <li>
                  <span className="tone-human">{s.humanReview}</span> human review
                </li>
              )}
              {s.failed > 0 && (
                <li>
                  <span className="tone-fail">{s.failed}</span> invalid or gate-failed
                </li>
              )}
              <li className="faint">{s.usage ? `${fmt(s.usage.calls)} model calls` : 'no model calls'}</li>
            </ul>
          </m.article>
        ))}
      </div>

      <Card title="Per unit" labelledBy="per-unit">
        <div className="table-scroll" tabIndex={0} role="region" aria-labelledby="per-unit">
          <table className="table matrix">
            <thead>
              <tr>
                <th scope="col">Scenario</th>
                {CONDITIONS.map((c) => (
                  <th key={c} scope="col" title={CONDITION_INFO[c].detail} className={RUN_OF[c] === run ? 'is-current' : undefined}>
                    <span className="mono">{c}</span>
                    <span className="th-sub">{CONDITION_INFO[c].name}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.scenarios.map((s) => (
                <tr key={s.id}>
                  <th scope="row">
                    <span className="mono">{s.label}</span>
                    <span className="th-sub">
                      {s.source} → {s.target}
                    </span>
                  </th>
                  {CONDITIONS.map((c) => (
                    <Cell key={c} unit={findUnit(data, s.id, c)} current={RUN_OF[c] === run} />
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="footnote">
          Tokens are the provider&apos;s counts: input, output and the reasoning share of the output. The provider
          total equals input + output on {totals.inputPlusOutput} of {totals.checked} calls that recorded a total, so
          output includes reasoning there; v0.4 did not record totals. v0.5 tokens cover the repair calls only; attempt 0 reused the recorded v0.4 reply.
          Oracle counts are checks O1 to O7.
        </p>
      </Card>

      <div className="two-col">
        <Card title="Exposure tags (pre-registered)" labelledBy="exposure">
          <dl className="legend">
            {exposures.map((e) => (
              <div key={e}>
                <dt>
                  <Chip>{e}</Chip>
                </dt>
                <dd>{EXPOSURE_INFO[e] ?? 'No description recorded.'}</dd>
              </div>
            ))}
          </dl>
          <p className="footnote">
            Tags mark where a failure was not the model&apos;s alone. They were fixed before the run, together with
            the one predicted outcome.
          </p>
        </Card>
        <Card title="As proposed: blocked before any model call" labelledBy="as-proposed">
          <table className="table table--compact">
            <thead>
              <tr>
                <th scope="col">Unit</th>
                <th scope="col">Outcome</th>
                <th scope="col">Blocking fields</th>
              </tr>
            </thead>
            <tbody>
              {asProposed.map((u) => (
                <tr key={u.key}>
                  <td className="mono">
                    {data.scenarios.find((s) => s.id === u.scenario)?.label} {u.condition}
                  </td>
                  <td>
                    <Badge tone={OUTCOME_TONE[outcomeOf(u)]}>{u.status}</Badge>
                  </td>
                  <td className="mono small">{u.blockedReasons.join('; ')}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="footnote">
            The real v0.3 mapping proposals, with no human edit. <a href={href('review')}>Why they block</a>.
          </p>
        </Card>
      </div>
    </div>
  )
}
