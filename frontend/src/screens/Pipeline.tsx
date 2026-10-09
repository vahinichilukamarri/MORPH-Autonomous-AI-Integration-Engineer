import { lazy, Suspense, useEffect, useMemo, useState } from 'react'

import { loadLiveUnit } from '../api/live'
import { MODE } from '../api/mode'
import { navigate } from '../app/router'
import { setRun, useRun } from '../app/runs'
import { useReducedMotion, useTheme } from '../app/theme'
import { useAsync } from '../app/useAsync'
import { Dialog } from '../components/Dialog'
import { Badge, Card, EmptyState, ErrorState, PageHead, Skeleton } from '../components/ui'
import { OUTCOME_LABEL, OUTCOME_TONE, outcomeOf, RUN_NAME, runUnits, unitTitle } from '../data/derive'
import { pipelineFromUnit, STATE_TONE, type Stage, type StageId } from '../data/pipeline'
import { recorded } from '../data/recorded'
import type { Milestone, Unit } from '../data/types'

const PipelineGraph = lazy(() => import('./PipelineGraph'))

const LEGEND: { tone: string; label: string }[] = [
  { tone: 'ok', label: 'passed' },
  { tone: 'fail', label: 'failed' },
  { tone: 'accent', label: 'looped' },
  { tone: 'blocked', label: 'blocked' },
  { tone: 'human', label: 'needs a person' },
  { tone: 'incorrect', label: 'incorrect' },
  { tone: 'skipped', label: 'not reached' },
]

function defaultUnit(run: Milestone): Unit {
  const units = runUnits(recorded, run)
  return (
    units.find((u) => u.status === 'READY' && u.integrationCorrect === false) ??
    units.find((u) => u.inputSet === 'approved' && u.condition !== 'D') ??
    units[0] ??
    recorded.units[0]
  )
}

function StageDetail({ stage }: { stage: Stage }) {
  return (
    <div className="detail">
      <div className="detail__head">
        <Badge tone={STATE_TONE[stage.state]}>{stage.stateLabel}</Badge>
        <span className="muted small">{stage.subtitle}</span>
      </div>
      {stage.details.length ? (
        <ul className="detail__list">
          {stage.details.map((d, i) => (
            <li key={i} className="mono small">
              {d}
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState title="Nothing recorded for this stage" />
      )}
    </div>
  )
}

function PipelineView({ unit, title }: { unit: Unit; title: string }) {
  const view = useMemo(() => pipelineFromUnit(unit), [unit])
  const [open, setOpen] = useState<StageId | null>(null)
  const theme = useTheme()
  const reducedMotion = useReducedMotion()
  const stage = view.stages.find((s) => s.id === open)
  const outcome = outcomeOf(unit)

  return (
    <>
      <Card
        title={title}
        labelledBy="pipeline-graph"
        actions={<Badge tone={OUTCOME_TONE[outcome]}>{OUTCOME_LABEL[outcome]}</Badge>}
        className="pipeline"
      >
        <Suspense fallback={<Skeleton lines={6} label="Loading graph" />}>
          <PipelineGraph view={view} selected={open} onSelect={setOpen} theme={theme} reducedMotion={reducedMotion} />
        </Suspense>
        <ul className="legend-row" aria-label="Legend">
          {LEGEND.map((l) => (
            <li key={l.tone}>
              <span className={`legend-dot legend-dot--${l.tone}`} aria-hidden="true" />
              {l.label}
            </li>
          ))}
          <li>
            <span className="legend-path" aria-hidden="true" />
            path the run took
          </li>
        </ul>
        <h3 className="subhead">Stages</h3>
        <ol className="stage-list" aria-label="Stages; select one for its recorded detail">
          {view.stages.map((s) => (
            <li key={s.id}>
              <button
                type="button"
                className={`stage-chip stage-chip--${STATE_TONE[s.state]}`}
                aria-haspopup="dialog"
                onClick={() => setOpen(s.id)}
              >
                <span className="stage-chip__dot" aria-hidden="true" />
                {s.title}
                <span className="visually-hidden">: {s.stateLabel}</span>
              </button>
            </li>
          ))}
        </ol>
      </Card>
      <Dialog
        open={stage !== undefined}
        onClose={() => setOpen(null)}
        title={stage ? `${stage.title}` : 'Stage'}
        description={stage ? `${title} · ${stage.stateLabel}` : undefined}
        variant="drawer"
      >
        {stage && <StageDetail stage={stage} />}
      </Dialog>
    </>
  )
}

function UnitPicker({ unit }: { unit: Unit }) {
  const data = recorded
  const units = runUnits(data, unit.milestone)
  return (
    <div className="picker" role="group" aria-label="Choose a unit">
      <label>
        <span>Scenario</span>
        <select
          className="input"
          value={unit.scenario}
          onChange={(e) => {
            const next =
              units.find((u) => u.scenario === e.target.value && u.condition === unit.condition && u.inputSet === unit.inputSet) ??
              units.find((u) => u.scenario === e.target.value)
            if (next) navigate('pipeline', { unit: next.key })
          }}
        >
          {data.scenarios.map((s) => (
            <option key={s.id} value={s.id}>
              {s.label} · {s.source} → {s.target}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span>Unit in {RUN_NAME[unit.milestone]}</span>
        <select className="input" value={unit.key} onChange={(e) => navigate('pipeline', { unit: e.target.value })}>
          {units
            .filter((u) => u.scenario === unit.scenario)
            .map((u) => (
              <option key={u.key} value={u.key}>
                {unitTitle(data, u)} · {u.status}
              </option>
            ))}
        </select>
      </label>
    </div>
  )
}

function LivePipeline() {
  const [draft, setDraft] = useState({ id: '', version: '' })
  const [query, setQuery] = useState<{ id: number; version: number | null } | null>(null)
  return (
    <Card title="Live: an integration from the API" labelledBy="live-pipeline">
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault()
          const id = Number(draft.id)
          const version = draft.version.trim() === '' ? null : Number(draft.version)
          setQuery(Number.isInteger(id) && id > 0 ? { id, version } : null)
        }}
      >
        <label htmlFor="int-id">Integration id</label>
        <input id="int-id" className="input" inputMode="numeric" value={draft.id} onChange={(e) => setDraft({ ...draft, id: e.target.value })} />
        <label htmlFor="int-version">Version (latest if empty)</label>
        <input
          id="int-version"
          className="input"
          inputMode="numeric"
          value={draft.version}
          onChange={(e) => setDraft({ ...draft, version: e.target.value })}
        />
        <button type="submit" className="btn btn--secondary">
          Load
        </button>
      </form>
      {query ? <LiveResult id={query.id} version={query.version} /> : <EmptyState title="No integration selected" />}
    </Card>
  )
}

function LiveResult({ id, version }: { id: number; version: number | null }) {
  const [state, reload] = useAsync((signal) => loadLiveUnit(id, version, signal), [id, version])
  if (state.kind === 'loading') return <Skeleton lines={4} />
  if (state.kind === 'error') return <ErrorState error={state.error} onRetry={reload} />
  return <PipelineView unit={state.data} title={`Integration ${id} · ${state.data.scenario} · ${state.data.condition}`} />
}

export default function Pipeline({ params }: { params: URLSearchParams }) {
  const run = useRun()
  const linked = recorded.units.find((u) => u.key === params.get('unit'))
  const unit = linked ?? defaultUnit(run)

  // A link to a unit selects that unit's run, so the badge and picker match what is shown.
  useEffect(() => {
    if (linked && linked.milestone !== run) setRun(linked.milestone)
  }, [linked, run])

  return (
    <div className="page">
      <PageHead
        eyebrow="Pipeline"
        title="Pipeline"
        lede="One unit through every stage. Colour is the state each stage ended in; the moving path is the route the run actually took, including the repair loop. Select a stage for its recorded detail."
        aside={<UnitPicker unit={unit} />}
      />
      <PipelineView key={unit.key} unit={unit} title={unitTitle(recorded, unit)} />
      {MODE === 'live' && <LivePipeline />}
    </div>
  )
}
