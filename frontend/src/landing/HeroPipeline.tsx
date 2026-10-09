import { useReducedMotion } from '../app/theme'

/**
 * The hero illustration: the pipeline as a track. A packet runs discover → map → review gate →
 * generate and verify, fails once, takes the repair loop, passes and ends at READY, and a dashed spur
 * shows the stop at the review gate. Pure SVG with SMIL motion; with reduced motion it is a still
 * diagram with the packet resting at READY.
 */
const TRACK = 'M70 150 H470'
const LOOP = 'M470 150 C 520 150 530 242 470 242 C 410 242 420 150 470 150'
const EXIT = 'M470 150 C 520 150 540 104 600 104'
const RUN = `M70 150 H470 C 520 150 530 242 470 242 C 410 242 420 150 470 150 C 520 150 540 104 600 104`

const STATIONS = [
  { x: 70, label: 'Discover', sub: 'contracts' },
  { x: 180, label: 'Map', sub: 'fields' },
  { x: 300, label: 'Review', sub: 'gate', gate: true },
  { x: 470, label: 'Generate', sub: '+ verify' },
] as const

export function HeroPipeline() {
  const reduced = useReducedMotion()
  return (
    <figure className="hero-viz">
      <svg viewBox="0 0 680 300" role="img" aria-labelledby="hero-viz-title" className="hero-viz__svg">
        <title id="hero-viz-title">
          The MORPH pipeline: discover, map, a review gate, generate and verify in a sandbox, a bounded repair loop, then READY or
          a hand-off to a person.
        </title>
        <defs>
          <pattern id="hv-dots" width="30" height="26" patternUnits="userSpaceOnUse">
            <circle cx="10" cy="10" r="1" />
          </pattern>
          <radialGradient id="hv-glow">
            <stop offset="0%" stopColor="var(--path)" stopOpacity="0.55" />
            <stop offset="100%" stopColor="var(--path)" stopOpacity="0" />
          </radialGradient>
          <linearGradient id="hv-track" x1="0" x2="1">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.25" />
            <stop offset="100%" stopColor="var(--path)" stopOpacity="0.9" />
          </linearGradient>
        </defs>

        <rect width="680" height="300" fill="url(#hv-dots)" className="hero-viz__grid" />

        <path d={TRACK} className="hero-viz__rail" />
        <path d={TRACK} className="hero-viz__flow" stroke="url(#hv-track)" />
        <path d={LOOP} className="hero-viz__loop" />
        <path d={EXIT} className="hero-viz__exit" />
        <path d="M300 150 V 60 H 600" className="hero-viz__spur" />

        {STATIONS.map((s) => (
          <g key={s.label} transform={`translate(${s.x} 150)`} className="hero-viz__station">
            {'gate' in s ? (
              <rect x="-15" y="-15" width="30" height="30" rx="5" transform="rotate(45)" className="hero-viz__gate" />
            ) : (
              <circle r="15" className="hero-viz__node" />
            )}
            <circle r="4" className="hero-viz__core" />
            <text y="40" textAnchor="middle" className="hero-viz__label">
              {s.label}
            </text>
            <text y="56" textAnchor="middle" className="hero-viz__sub">
              {s.sub}
            </text>
          </g>
        ))}

        <g transform="translate(470 242)" className="hero-viz__station">
          <circle r="12" className="hero-viz__node hero-viz__node--loop" />
          <text x="0" y="36" textAnchor="middle" className="hero-viz__label">
            Repair
          </text>
          <text x="0" y="52" textAnchor="middle" className="hero-viz__sub">
            bounded
          </text>
        </g>

        <g transform="translate(600 104)" className="hero-viz__end hero-viz__end--ok">
          <rect x="-4" y="-15" width="72" height="30" rx="15" />
          <text x="32" y="5" textAnchor="middle">
            READY
          </text>
        </g>
        <g transform="translate(600 60)" className="hero-viz__end hero-viz__end--blocked">
          <rect x="-4" y="-13" width="72" height="26" rx="13" />
          <text x="32" y="4" textAnchor="middle">
            BLOCKED
          </text>
        </g>
        <g transform="translate(540 242)" className="hero-viz__end hero-viz__end--human">
          <rect x="0" y="-13" width="128" height="26" rx="13" />
          <text x="64" y="4" textAnchor="middle">
            HUMAN_REVIEW
          </text>
        </g>
        <path d="M482 242 H 540" className="hero-viz__spur hero-viz__spur--human" />

        <g className="hero-viz__packet" transform={reduced ? 'translate(600 104)' : undefined}>
          <circle r="16" fill="url(#hv-glow)" />
          <circle r="5" className="hero-viz__dot" />
          {!reduced && (
            <animateMotion dur="7s" repeatCount="indefinite" path={RUN} keyPoints="0;1;1" keyTimes="0;0.86;1" calcMode="linear" />
          )}
        </g>
      </svg>
      <figcaption className="hero-viz__caption">
        Every run ends in <span className="tone-ok">READY</span>, <span className="tone-blocked">BLOCKED_PENDING_REVIEW</span> or{' '}
        <span className="tone-human">HUMAN_REVIEW_REQUIRED</span>. Correctness is then graded by a hidden oracle.
      </figcaption>
    </figure>
  )
}
