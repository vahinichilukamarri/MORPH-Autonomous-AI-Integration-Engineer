import { lazy, Suspense, useMemo, useState } from 'react'

import { loadLiveUnit } from '../api/live'
import { MODE } from '../api/mode'
import { navigate } from '../app/router'
import { useAppliedTheme, useReducedMotion } from '../app/theme'
import { useAsync } from '../app/useAsync'
import { Badge, EmptyState, ErrorState, PageHead, Panel, Skeleton } from '../components/ui'
import { OUTCOME_LABEL, OUTCOME_TONE, outcomeOf, unitTitle } from '../data/derive'
import { pipelineFromUnit, STATE_TONE, type StageId } from '../data/pipeline'
import { recorded } from '../data/recorded'
import type { Unit } from '../data/types'

const PipelineGraph = lazy(() => import('./PipelineGraph'))

const DEFAULT_UNIT = recorded.units.find((u) => u.milestone === 'v0.5' && u.status === 'READY') ?? recorded.units[0]

function PipelineView({ unit, title }: { unit: Unit; title: string }) {
  const view = useMemo(() => pipelineFromUnit(unit), [unit])
  const [selected, setSelected] = useState<StageId>('terminal')
  const theme = useAppliedTheme()
  const reducedMotion = useReducedMotion()
  const stage = view.stages.find((s) => s.id === selected) ?? view.stages[0]
  const outcome = outcomeOf(unit)

  return (
    <div className="pipeline">
      <Panel
        title={title}
        labelledBy="pipeline-graph"
        actions={<Badge tone={OUTCOME_TONE[outcome]}>{OUTCOME_LABEL[outcome]}</Badge>}
        className="pipeline__graph"
      >
        <Suspense fallback={<Skeleton lines={5} label="Loading graph" />}>
          <PipelineGraph view={view} selected={selected} onSelect={setSelected} theme={theme} reducedMotion={reducedMotion} />
        </Suspense>
        <ol className="stage-list" aria-label="Stages">
          {view.stages.map((s) => (
            <li key={s.id}>
              <button
                type="button"
                className={`stage-list__item stage-list__item--${STATE_TONE[s.state]}`}
                aria-pressed={s.id === selected}
                onClick={() => setSelected(s.id)}
              >
                <span className="stage-list__dot" aria-hidden="true" />
                {s.title}
                <span className="visually-hidden">: {s.stateLabel}</span>
              </button>
            </li>
          ))}
        </ol>
      </Panel>
      <Panel title="Stage detail" labelledBy="stage-detail" className="pipeline__detail">
        <div className="detail" aria-live="polite">
          <div className="detail__head">
            <h3 className="detail__title">{stage.title}</h3>
            <Badge tone={STATE_TONE[stage.state]}>{stage.stateLabel}</Badge>
          </div>
          <p className="muted">{stage.subtitle}</p>
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
      </Panel>
    </div>
  )
}

function UnitPicker({ unit }: { unit: Unit }) {
  const data = recorded
  return (
    <div className="picker" role="group" aria-label="Choose a unit">
      <label>
        <span>Scenario</span>
        <select
          className="input"
          value={unit.scenario}
          onChange={(e) => {
            const next =
              data.units.find((u) => u.scenario === e.target.value && u.condition === unit.condition && u.inputSet === unit.inputSet) ??
              data.units.find((u) => u.scenario === e.target.value)
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
        <span>Unit</span>
        <select className="input" value={unit.key} onChange={(e) => navigate('pipeline', { unit: e.target.value })}>
          {data.units
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
    <Panel title="Live: an integration from the API" labelledBy="live-pipeline">
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
        <button type="submit" className="button">
          Load
        </button>
      </form>
      {query ? <LiveResult id={query.id} version={query.version} /> : <EmptyState title="No integration selected" />}
    </Panel>
  )
}

function LiveResult({ id, version }: { id: number; version: number | null }) {
  const [state, reload] = useAsync((signal) => loadLiveUnit(id, version, signal), [id, version])
  if (state.kind === 'loading') return <Skeleton lines={4} />
  if (state.kind === 'error') return <ErrorState error={state.error} onRetry={reload} />
  return <PipelineView unit={state.data} title={`Integration ${id} · ${state.data.scenario} · ${state.data.condition}`} />
}

export default function Pipeline({ params }: { params: URLSearchParams }) {
  const unit = recorded.units.find((u) => u.key === params.get('unit')) ?? DEFAULT_UNIT
  return (
    <div className="page">
      <PageHead
        title="Pipeline"
        lede="One unit through the pipeline. Colour is the state each stage ended in; animated edges are the path the run actually took, including the repair loop. Select a stage for its recorded detail."
        aside={<UnitPicker unit={unit} />}
      />
      <PipelineView key={unit.key} unit={unit} title={unitTitle(recorded, unit)} />
      {MODE === 'live' && <LivePipeline />}
    </div>
  )
}
