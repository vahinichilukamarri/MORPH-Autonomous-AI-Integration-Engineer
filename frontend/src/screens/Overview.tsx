import * as m from 'motion/react-m'

import { href, SCREENS } from '../app/router'
import { setRun, useRun } from '../app/runs'
import { Icon } from '../components/Icon'
import { Badge, Card, Metric, PageHead } from '../components/ui'
import { CONDITION_INFO, fmt, RUN_NAME, runsPerUnit, summarise, type ConditionSummary } from '../data/derive'
import { recorded } from '../data/recorded'
import type { Condition, Milestone } from '../data/types'
import { CountUp } from '../motion/CountUp'
import { DURATION, EASE, STAGGER } from '../motion/tokens'

const BY_RUN: Record<Milestone, Condition[]> = { 'v0.4': ['D', 'L1', 'L2'], 'v0.5': ['L1R', 'L2R'] }

function ConditionMetric({ s }: { s: ConditionSummary }) {
  const tone = s.readyIncorrect > 0 ? 'incorrect' : s.ready === s.units ? 'ok' : s.humanReview > 0 ? 'human' : 'fail'
  return (
    <Metric
      tone={tone}
      label={
        <>
          <span className="mono">{s.condition}</span> · {CONDITION_INFO[s.condition].name}
        </>
      }
      value={
        <>
          <CountUp value={s.ready} />
          <span className="metric__of">/{s.units}</span> <span className="metric__unit">READY</span>
        </>
      }
      sub={
        <span className="metric__facts">
          <span>
            <span className="tone-ok">{s.correct}</span> oracle-correct
          </span>
          {s.readyIncorrect > 0 && (
            <span>
              <span className="tone-incorrect">{s.readyIncorrect}</span> READY but incorrect
            </span>
          )}
          {s.humanReview > 0 && (
            <span>
              <span className="tone-human">{s.humanReview}</span> handed to a person
            </span>
          )}
          {s.failed > 0 && (
            <span>
              <span className="tone-fail">{s.failed}</span> invalid or gate-failed
            </span>
          )}
        </span>
      }
    />
  )
}

export default function Overview() {
  const data = recorded
  const run = useRun()
  const n = runsPerUnit(data)
  const order: Milestone[] = run === 'v0.5' ? ['v0.5', 'v0.4'] : ['v0.4', 'v0.5']
  const l1r = summarise(data, 'L1R')
  const runInfo = data.runs.find((r) => r.milestone === run)

  return (
    <div className="page">
      <PageHead
        eyebrow="Overview"
        title="An integration engineer that shows its work"
        lede={
          <>
            MORPH reads two systems&apos; OpenAPI contracts, proposes how their fields map, stops at a review gate when a
            required field is uncertain, generates the integration, checks it in a locked-down sandbox and repairs failures
            within a fixed budget. The model only proposes; deterministic code validates, gates and executes.
          </>
        }
      />

      <div className="callouts">
        <div className="callout callout--incorrect">
          <Badge tone="incorrect">READY is not correctness</Badge>
          <p>
            READY means the static gate, generated tests and smoke test passed. Only the hidden oracle decides correctness:{' '}
            <strong>{l1r.readyIncorrect}</strong> L1R unit passed everything MORPH controls and still failed it.
          </p>
        </div>
        <div className="callout callout--human">
          <Badge tone="human">N={n} per unit</Badge>
          <p>
            One model, one run per unit, {data.scenarios.length} synthetic scenarios. Nothing here is statistically
            significant; a difference of one unit is noise.
          </p>
        </div>
      </div>

      {order.map((milestone) => (
        <section key={milestone} className={`run-block${milestone === run ? ' is-current' : ''}`} aria-labelledby={`run-${milestone}`}>
          <header className="run-block__head">
            <h2 id={`run-${milestone}`}>{RUN_NAME[milestone]}</h2>
            {milestone === run ? (
              <span className="pill pill--accent">Selected run</span>
            ) : (
              <button type="button" className="btn btn--ghost btn--sm" onClick={() => setRun(milestone)}>
                Select this run
              </button>
            )}
          </header>
          <ul className="metrics">
            {BY_RUN[milestone].map((c, i) => (
              <m.li
                key={c}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: DURATION.slow, ease: EASE.out, delay: i * STAGGER }}
              >
                <ConditionMetric s={summarise(data, c)} />
              </m.li>
            ))}
          </ul>
        </section>
      ))}

      <section aria-labelledby="explore" className="stack">
        <h2 id="explore" className="section-title">
          Explore the recorded runs
        </h2>
        <ul className="tiles">
          {SCREENS.filter((s) => s.id !== 'overview').map((s, i) => (
            <m.li
              key={s.id}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: DURATION.slow, ease: EASE.out, delay: 0.1 + i * STAGGER }}
            >
              <a className="tile" href={href(s.id)}>
                <span className="tile__icon">
                  <Icon name={s.icon} size={20} />
                </span>
                <span className="tile__title">{s.label}</span>
                <span className="tile__hint">{s.hint}</span>
                <span className="tile__key mono" aria-hidden="true">
                  {s.key}
                </span>
              </a>
            </m.li>
          ))}
        </ul>
      </section>

      <Card title="Data provenance" labelledBy="provenance">
        <div className="table-scroll" tabIndex={0} role="region" aria-labelledby="provenance">
          <table className="table">
            <caption className="visually-hidden">Recorded runs</caption>
            <thead>
              <tr>
                <th scope="col">Run</th>
                <th scope="col">Date</th>
                <th scope="col">Model</th>
                <th scope="col">Settings</th>
                <th scope="col">Harness</th>
                <th scope="col" className="num">
                  Declared post-run edits
                </th>
              </tr>
            </thead>
            <tbody>
              {data.runs.map((r) => (
                <tr key={r.milestone} className={r.milestone === runInfo?.milestone ? 'is-current' : undefined}>
                  <td>{RUN_NAME[r.milestone]}</td>
                  <td className="mono">{r.runDate}</td>
                  <td className="mono">
                    {r.provider} {r.model}
                  </td>
                  <td>{r.settings}</td>
                  <td className="mono">{r.harnessSha}</td>
                  <td className="num mono">{fmt(r.postRunEdits)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <details className="sources">
          <summary>
            Generated from {data.generatedFrom.length} committed files <span className="faint">(SHA-256, LF endings)</span>
          </summary>
          <ul>
            {data.generatedFrom.map((f) => (
              <li key={f.path}>
                <code>{f.path}</code> <code className="faint">{f.sha256.slice(0, 16)}…</code>
              </li>
            ))}
          </ul>
        </details>
      </Card>
    </div>
  )
}
