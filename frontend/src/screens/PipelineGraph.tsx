import '@xyflow/react/dist/style.css'

import {
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import { memo, useMemo } from 'react'

import type { Theme } from '../app/theme'
import { STATE_TONE, type PipelineView, type Stage, type StageId } from '../data/pipeline'

type StageNode = Node<{ stage: Stage; selected: boolean }, 'stage'>

const STEP = 160
const POSITIONS: Record<StageId, { x: number; y: number }> = {
  discovery: { x: 0, y: 0 },
  mapping: { x: STEP, y: 0 },
  review: { x: STEP * 2, y: 0 },
  codegen: { x: STEP * 3, y: 0 },
  validate: { x: STEP * 4, y: 0 },
  gates: { x: STEP * 5, y: 0 },
  sandbox: { x: STEP * 6, y: 0 },
  terminal: { x: STEP * 7, y: 0 },
  repair: { x: STEP * 5, y: 150 },
  oracle: { x: STEP * 7, y: 150 },
}

const StageCard = memo(function StageCard({ data }: NodeProps<StageNode>) {
  const { stage, selected } = data
  const tone = STATE_TONE[stage.state]
  return (
    <div className={`flow-node flow-node--${tone}${selected ? ' is-selected' : ''}`}>
      <Handle type="target" position={Position.Left} id="l" />
      <Handle type="target" position={Position.Bottom} id="b-in" />
      <Handle type="target" position={Position.Top} id="t-in" />
      <div className="flow-node__title">{stage.title}</div>
      <div className="flow-node__sub">{stage.subtitle}</div>
      <div className="flow-node__state">{stage.stateLabel}</div>
      <Handle type="source" position={Position.Right} id="r" />
      <Handle type="source" position={Position.Bottom} id="b" />
      <Handle type="source" position={Position.Top} id="t" />
    </div>
  )
})

const NODE_TYPES = { stage: StageCard }

function handles(source: StageId, target: StageId): { sourceHandle: string; targetHandle: string } {
  const s = POSITIONS[source]
  const t = POSITIONS[target]
  if (t.y > s.y) return { sourceHandle: 'b', targetHandle: 't-in' }
  if (t.y < s.y) return { sourceHandle: 't', targetHandle: 'b-in' }
  return { sourceHandle: 'r', targetHandle: 'l' }
}

export default function PipelineGraph({
  view,
  selected,
  onSelect,
  theme,
  reducedMotion,
}: {
  view: PipelineView
  selected: StageId
  onSelect: (id: StageId) => void
  theme: Theme
  reducedMotion: boolean
}) {
  const nodes = useMemo<StageNode[]>(
    () =>
      view.stages.map((stage) => ({
        id: stage.id,
        type: 'stage',
        position: POSITIONS[stage.id],
        data: { stage, selected: stage.id === selected },
        draggable: false,
        connectable: false,
        ariaLabel: `${stage.title}: ${stage.stateLabel}`,
      })),
    [view, selected],
  )
  const edges = useMemo<Edge[]>(
    () =>
      view.flows.map((f) => {
        const colour = !f.traversed
          ? 'var(--line-strong)'
          : f.kind === 'failure'
            ? 'var(--fail)'
            : f.kind === 'loop'
              ? 'var(--accent)'
              : f.kind === 'grade'
                ? 'var(--text-muted)'
                : 'var(--ok)'
        return {
          id: f.id,
          source: f.source,
          target: f.target,
          ...handles(f.source, f.target),
          type: 'smoothstep',
          animated: f.traversed && !reducedMotion && f.kind !== 'grade',
          className: f.traversed ? 'flow-edge is-traversed' : 'flow-edge',
          style: {
            stroke: colour,
            strokeWidth: f.traversed ? 2 : 1,
            strokeDasharray: f.kind === 'grade' ? '4 4' : undefined,
            opacity: f.traversed ? 1 : 0.45,
          },
          markerEnd: { type: MarkerType.ArrowClosed, color: colour, width: 14, height: 14 },
          focusable: false,
        }
      }),
    [view, reducedMotion],
  )

  return (
    <div className="flow" role="figure" aria-label="Pipeline graph; the stage buttons below list the same states">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        colorMode={theme}
        fitView
        fitViewOptions={{ padding: 0.08 }}
        nodesDraggable={false}
        nodesConnectable={false}
        nodesFocusable={false}
        edgesFocusable={false}
        zoomOnScroll={false}
        panOnScroll={false}
        preventScrolling={false}
        minZoom={0.3}
        onNodeClick={(_, node) => onSelect(node.id as StageId)}
      />
    </div>
  )
}
