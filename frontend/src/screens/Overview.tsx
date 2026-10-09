import { href, ROUTES } from '../app/router'
import { Badge, Metric, PageHead, Panel } from '../components/ui'
import { addUsage, fmt, runsPerUnit, summarise, type ConditionSummary } from '../data/derive'
import { recorded } from '../data/recorded'
import type { Usage } from '../data/types'

const STRIP = [
  'Discovery',
  'Mapping',
  'Review gate',
  'Codegen',
  'Static gate',
  'Sandbox',
  'Repair loop',
  'READY or HUMAN_REVIEW_REQUIRED',
]

function readyLine(s: ConditionSummary): string {
  return `${s.ready}/${s.units} READY`
}

export function Overview() {
  const data = recorded
  const [d, l1, l2, l1r, l2r] = (['D', 'L1', 'L2', 'L1R', 'L2R'] as const).map((c) => summarise(data, c))
  const repairUsage = addUsage(
    data.units.filter((u) => u.milestone === 'v0.5' && u.usage).map((u) => u.usage as Usage),
  )
  const n = runsPerUnit(data)
  const v05 = data.runs.find((r) => r.milestone === 'v0.5')

  return (
    <div className="page">
      <PageHead
        title="An integration engineer that shows its work"
        lede={
          <>
            MORPH reads two systems&apos; OpenAPI contracts, proposes how their fields map, stops at a human
            review gate when a required field is uncertain, generates the integration, checks it in a locked-down
            sandbox and repairs failures within a fixed budget. The model only proposes; deterministic code
            validates, gates and executes. A hidden oracle, never seen by the pipeline, decides correctness.
          </>
        }
      />

      <ol className="strip" aria-label="Pipeline stages">
        {STRIP.map((s, i) => (
          <li key={s} className="strip__step">
            <span className="strip__n mono">{String(i + 1).padStart(2, '0')}</span>
            {s}
          </li>
        ))}
      </ol>

      <section aria-labelledby="measured" className="stack">
        <div className="section-head">
          <h2 id="measured">What was measured</h2>
          <p className="muted">
            {data.scenarios.length} scenarios on approved mappings, N={n} run per unit. Nothing here is statistically significant; a difference of one
            unit is noise.
          </p>
        </div>
        <div className="metrics">
          <Metric label="D · deterministic" value={readyLine(d)} sub={`${d.correct} oracle-correct`} tone="ok" />
          <Metric label="L1 · model strategy" value={readyLine(l1)} sub="one shot, v0.4" tone={l1.ready ? 'ok' : 'fail'} />
          <Metric label="L2 · model module" value={readyLine(l2)} sub="one shot, v0.4" tone={l2.ready ? 'ok' : 'fail'} />
          <Metric
            label="L1R · strategy + repair"
            value={readyLine(l1r)}
            sub={
              <>
                {l1r.correct} correct · <span className="tone-incorrect">{l1r.readyIncorrect} READY but incorrect</span>
              </>
            }
            tone="incorrect"
          />
          <Metric
            label="L2R · module + repair"
            value={readyLine(l2r)}
            sub={`${l2r.humanReview} HUMAN_REVIEW_REQUIRED`}
            tone="human"
          />
          <Metric
            label="Repair calls (v0.5)"
            value={fmt(repairUsage.calls)}
            sub={`${fmt(repairUsage.input)} in · ${fmt(repairUsage.output)} out tokens`}
          />
        </div>
        <div className="callouts">
          <div className="callout">
            <Badge tone="incorrect">READY · incorrect</Badge>
            <p>
              READY means the static gate, generated tests and smoke test passed. It is not correctness. {l1r.readyIncorrect} L1R
              unit(s) passed everything MORPH controls and still failed the oracle, an outcome pre-registered before
              the run.
            </p>
          </div>
          <div className="callout">
            <Badge tone="human">HUMAN_REVIEW_REQUIRED</Badge>
            <p>
              After {v05?.maxRepairAttempts ?? '?'} repairs the loop stops and hands over to a person. Part of the L2
              failures trace to a contradiction between our own prompt and gate, kept frozen for a clean measurement.
            </p>
          </div>
        </div>
      </section>

      <section aria-labelledby="explore" className="stack">
        <h2 id="explore">Explore</h2>
        <ul className="cards">
          {ROUTES.filter((r) => r.id !== 'overview').map((r) => (
            <li key={r.id}>
              <a className="card" href={href(r.id)}>
                <span className="card__key mono" aria-hidden="true">
                  {r.key}
                </span>
                <span className="card__title">{r.label}</span>
                <span className="card__hint">{r.hint}</span>
              </a>
            </li>
          ))}
        </ul>
      </section>

      <Panel title="Data provenance" labelledBy="provenance">
        <table className="table table--compact">
          <caption className="visually-hidden">Recorded runs</caption>
          <thead>
            <tr>
              <th scope="col">Run</th>
              <th scope="col">Date</th>
              <th scope="col">Model</th>
              <th scope="col">Settings</th>
              <th scope="col">Harness</th>
              <th scope="col">Declared post-run edits</th>
            </tr>
          </thead>
          <tbody>
            {data.runs.map((r) => (
              <tr key={r.milestone}>
                <td>{r.milestone === 'v0.4' ? 'v0.4 codegen' : `v0.5 repair (${r.startMode ?? '?'} start)`}</td>
                <td className="mono">{r.runDate}</td>
                <td className="mono">
                  {r.provider} {r.model}
                </td>
                <td>{r.settings}</td>
                <td className="mono">{r.harnessSha}</td>
                <td className="num">{r.postRunEdits}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <details className="sources">
          <summary>Generated from {data.generatedFrom.length} committed files (SHA-256, LF endings)</summary>
          <ul>
            {data.generatedFrom.map((f) => (
              <li key={f.path}>
                <code>{f.path}</code> <code className="faint">{f.sha256.slice(0, 16)}…</code>
              </li>
            ))}
          </ul>
        </details>
      </Panel>
    </div>
  )
}
