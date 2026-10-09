import '../styles/landing.css'

import { useEffect, useState, type MouseEvent } from 'react'

import { href } from '../app/router'
import { toggleTheme, useReducedMotion, useTheme } from '../app/theme'
import { Brand } from '../components/Brand'
import { Icon } from '../components/Icon'
import { Badge, InlineMarkdown, repoLink } from '../components/ui'
import { CONDITION_INFO, RUN_NAME, runsPerUnit, summarise, type ConditionSummary } from '../data/derive'
import { recorded } from '../data/recorded'
import type { Condition, Milestone, MilestoneState } from '../data/types'
import { CountUp } from '../motion/CountUp'
import { Reveal } from '../motion/Reveal'
import { STAGGER } from '../motion/tokens'
import { HeroPipeline } from './HeroPipeline'
import { PolicyPreview } from './PolicyPreview'

const SECTIONS = [
  { id: 'how', label: 'How it works' },
  { id: 'results', label: 'Results' },
  { id: 'guarantees', label: 'Guarantees' },
  { id: 'policy', label: 'Policy' },
  { id: 'roadmap', label: 'Roadmap' },
] as const

const RESULT_GROUPS: { run: Milestone; conditions: Condition[] }[] = [
  { run: 'v0.4', conditions: ['D', 'L1', 'L2'] },
  { run: 'v0.5', conditions: ['L1R', 'L2R'] },
]

const STATE_LABEL: Record<MilestoneState, string> = { done: 'Done', in_progress: 'In progress', planned: 'Planned' }
const STATE_TONE = { done: 'ok', in_progress: 'accent', planned: 'skipped' } as const

/** Scrolls to a section without touching the hash, which belongs to the router. */
function useSectionLinks(reduced: boolean) {
  return (e: MouseEvent<HTMLAnchorElement>, id: string) => {
    e.preventDefault()
    const target = document.getElementById(id)
    if (!target) return
    target.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' })
    target.querySelector<HTMLElement>('h2')?.focus({ preventScroll: true })
  }
}

function ResultCard({ s }: { s: ConditionSummary }) {
  const tone = s.readyIncorrect > 0 ? 'incorrect' : s.ready === s.units ? 'ok' : s.humanReview > 0 ? 'human' : 'fail'
  return (
    <article className={`rcard rcard--${tone}`}>
      <header className="rcard__head">
        <span className="rcard__cond mono">{s.condition}</span>
        <span className="rcard__name">{CONDITION_INFO[s.condition].name}</span>
      </header>
      <div className="rcard__big">
        <CountUp value={s.ready} />
        <span className="rcard__of">/{s.units}</span>
        <span className="rcard__unit">READY</span>
      </div>
      <ul className="rcard__facts">
        <li>
          <span className="tone-ok">{s.correct}</span> oracle-correct
        </li>
        {s.readyIncorrect > 0 && (
          <li className="rcard__incorrect">
            <span className="tone-incorrect">{s.readyIncorrect}</span> READY but incorrect
          </li>
        )}
        {s.humanReview > 0 && (
          <li>
            <span className="tone-human">{s.humanReview}</span> handed to a person
          </li>
        )}
        {s.failed > 0 && (
          <li>
            <span className="tone-fail">{s.failed}</span> invalid or gate-failed
          </li>
        )}
      </ul>
      <p className="rcard__detail">{CONDITION_INFO[s.condition].detail}</p>
    </article>
  )
}

export default function Landing() {
  const data = recorded
  const site = data.site
  const theme = useTheme()
  const reduced = useReducedMotion()
  const scrollTo = useSectionLinks(reduced)
  const [menuOpen, setMenuOpen] = useState(false)
  const n = runsPerUnit(data)
  const maxRepairs = data.runs.find((r) => r.milestone === 'v0.5')?.maxRepairAttempts
  const l1r = summarise(data, 'L1R')

  useEffect(() => {
    document.title = 'MORPH · autonomous integration engineer'
  }, [])

  const steps = [
    {
      n: '01',
      title: 'Discover',
      body: 'Both OpenAPI contracts are parsed into one internal system model, and every field is indexed for retrieval.',
      tag: 'contracts → system model',
    },
    {
      n: '02',
      title: 'Map',
      body: 'A model proposes how each target field is filled. Deterministic validation and a confidence score decide what can be trusted.',
      tag: 'proposals + confidence',
    },
    {
      n: '03',
      title: 'Review gate',
      body: 'If a required field is still uncertain, the run stops before any code exists. No model call, nothing partial ships.',
      tag: 'BLOCKED_PENDING_REVIEW',
    },
    {
      n: '04',
      title: 'Generate and verify',
      body: 'Code is generated from approved mappings, then must pass AST rules, ruff, mypy strict, generated tests and a smoke test inside a locked-down sandbox.',
      tag: 'static gate + sandbox',
    },
    {
      n: '05',
      title: 'Repair',
      body: `Each failure becomes structured feedback for a bounded repair loop of at most ${maxRepairs ?? '?'} attempts. Then READY, or a hand-off to a person.`,
      tag: 'READY · HUMAN_REVIEW_REQUIRED',
    },
  ]

  return (
    <div className="landing">
      <a
        className="skip-link"
        href="#top"
        onClick={(e) => {
          e.preventDefault()
          document.getElementById('top')?.focus()
        }}
      >
        Skip to content
      </a>
      <header className="lnav">
        <div className="lnav__inner">
          <a href="#/" className="lnav__brand" aria-label="MORPH home">
            <Brand />
          </a>
          <nav aria-label="Sections" className={`lnav__links${menuOpen ? ' is-open' : ''}`} id="lnav-links">
            {SECTIONS.map((s) => (
              <a
                key={s.id}
                href={`#${s.id}`}
                onClick={(e) => {
                  setMenuOpen(false)
                  scrollTo(e, s.id)
                }}
              >
                {s.label}
              </a>
            ))}
          </nav>
          <div className="lnav__actions">
            <button
              type="button"
              className="btn btn--icon btn--ghost"
              onClick={toggleTheme}
              aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
            >
              <Icon name={theme === 'dark' ? 'sun' : 'moon'} />
            </button>
            <a className="btn btn--primary btn--sm lnav__cta" href={href('overview')}>
              Open live demo
            </a>
            <button
              type="button"
              className="btn btn--icon btn--ghost lnav__menu"
              aria-label="Sections"
              aria-expanded={menuOpen}
              aria-controls="lnav-links"
              onClick={() => setMenuOpen((o) => !o)}
            >
              <Icon name={menuOpen ? 'close' : 'menu'} />
            </button>
          </div>
        </div>
      </header>

      <main id="top" tabIndex={-1}>
        <section className="hero" aria-labelledby="hero-title">
          <div className="hero__copy">
            <p className="eyebrow">Autonomous integration engineer</p>
            <h1 id="hero-title" className="hero__title">
              Writes, tests and repairs the code that connects two APIs.
            </h1>
            <p className="hero__sub">
              Give MORPH two OpenAPI contracts. It maps the fields, generates the integration, verifies it in a locked-down
              sandbox and repairs what fails, and it stops for a person whenever it is not sure.
            </p>
            <div className="hero__ctas">
              <a className="btn btn--primary btn--lg" href={href('overview')}>
                Open live demo <Icon name="arrowRight" />
              </a>
              <a className="btn btn--secondary btn--lg" href={site.repoUrl} target="_blank" rel="noreferrer">
                <Icon name="github" /> View on GitHub
              </a>
            </div>
            <p className="hero__note">
              The demo replays recorded runs in your browser: no sign-up, no backend, no model calls.
            </p>
          </div>
          <HeroPipeline />
        </section>

        <section id="how" className="lsection" aria-labelledby="how-title">
          <div className="lsection__head">
            <p className="eyebrow">How it works</p>
            <h2 id="how-title" tabIndex={-1}>
              Five stages. The model proposes; deterministic code decides.
            </h2>
            <p className="lsection__lede">
              A model is used only where judgement is needed. Parsing, validation, gating, test execution and every state
              transition are plain code.
            </p>
          </div>
          <ol className="steps">
            {steps.map((s, i) => (
              <Reveal as="li" key={s.n} className="step" delay={i * STAGGER * 2}>
                <span className="step__n mono">{s.n}</span>
                <h3 className="step__title">{s.title}</h3>
                <p className="step__body">{s.body}</p>
                <span className="step__tag mono">{s.tag}</span>
              </Reveal>
            ))}
          </ol>
        </section>

        <section id="results" className="lsection" aria-labelledby="results-title">
          <div className="lsection__head">
            <p className="eyebrow">The honest results</p>
            <h2 id="results-title" tabIndex={-1}>
              What happened when it ran, including what failed.
            </h2>
            <p className="lsection__lede">
              {data.scenarios.length} scenarios on human-approved mappings, model {data.runs[0].model} on {data.runs[0].provider}. Every
              number below is generated from the committed result files.
            </p>
          </div>
          <div className="caveats" role="note">
            <div className="caveat caveat--human">
              <strong>N={n} per unit.</strong> One model, one run per unit, synthetic scenarios. Nothing here is statistically
              significant; a one-unit difference is noise.
            </div>
            <div className="caveat caveat--incorrect">
              <strong>READY is not correctness.</strong> READY means the gates, generated tests and smoke test passed. Only the
              hidden oracle decides correctness, and {l1r.readyIncorrect} L1R unit reached READY and still failed it.
            </div>
          </div>
          {RESULT_GROUPS.map((g) => (
            <div key={g.run} className="rgroup">
              <h3 className="rgroup__title">{RUN_NAME[g.run]}</h3>
              <div className="rgroup__cards">
                {g.conditions.map((c, i) => (
                  <Reveal key={c} delay={i * STAGGER * 2}>
                    <ResultCard s={summarise(data, c)} />
                  </Reveal>
                ))}
              </div>
            </div>
          ))}
          <Reveal className="findings">
            <h3 className="rgroup__title">Findings, as written in the README</h3>
            <ul>
              {site.findings.map((f) => (
                <li key={f.headline}>
                  <strong>{f.headline}</strong> <InlineMarkdown text={f.detail} repoUrl={site.repoUrl} />
                </li>
              ))}
            </ul>
          </Reveal>
          <p className="lsection__more">
            <a className="btn btn--secondary" href={href('results')}>
              Every unit, attempt and token count <Icon name="arrowRight" />
            </a>
          </p>
        </section>

        <section id="guarantees" className="lsection" aria-labelledby="guarantees-title">
          <div className="lsection__head">
            <p className="eyebrow">Guarantees</p>
            <h2 id="guarantees-title" tabIndex={-1}>
              Rules enforced in code, not in prompts.
            </h2>
            <p className="lsection__lede">Each one links to the code or test that enforces it.</p>
          </div>
          <ul className="guarantees">
            {site.guarantees.map((g, i) => (
              <Reveal as="li" key={g.title} className="guarantee" delay={(i % 3) * STAGGER * 2}>
                <h3 className="guarantee__title">{g.title}</h3>
                <p className="guarantee__claim">
                  <InlineMarkdown text={g.claim} repoUrl={site.repoUrl} />
                </p>
                <p className="guarantee__where">
                  <span className="faint">Enforced in</span>{' '}
                  {g.enforcedIn.map((e, k) => (
                    <span key={e.path}>
                      {k > 0 && ', '}
                      <a href={repoLink(site.repoUrl, e.path)} target="_blank" rel="noreferrer" className="mono">
                        {e.label}
                      </a>
                    </span>
                  ))}
                </p>
              </Reveal>
            ))}
          </ul>
        </section>

        <section id="policy" className="lsection" aria-labelledby="policy-title">
          <div className="lsection__head">
            <p className="eyebrow">
              Policy and audit <span className="preview-tag">Preview</span>
            </p>
            <h2 id="policy-title" tabIndex={-1}>
              Planned: every tool call gated, logged and hash-chained.
            </h2>
            <p className="lsection__lede">
              v0.6 is in progress: capabilities become MCP tools behind a deterministic policy layer, an append-only audit log
              and human approvals. This preview uses illustrative data in the planned shapes, not a recorded run.
            </p>
          </div>
          <Reveal>
            <PolicyPreview />
          </Reveal>
        </section>

        <section id="roadmap" className="lsection" aria-labelledby="roadmap-title">
          <div className="lsection__head">
            <p className="eyebrow">Roadmap</p>
            <h2 id="roadmap-title" tabIndex={-1}>
              Built one milestone at a time.
            </h2>
            <p className="lsection__lede">Status as recorded in the README on this branch.</p>
          </div>
          <ol className="roadmap">
            {site.roadmap.map((r, i) => (
              <Reveal as="li" key={r.tag} className={`roadmap__item roadmap__item--${r.state}`} delay={i * STAGGER}>
                <span className="roadmap__marker" aria-hidden="true" />
                <div className="roadmap__head">
                  <span className="mono roadmap__tag">{r.tag}</span>
                  <Badge tone={STATE_TONE[r.state]}>{STATE_LABEL[r.state]}</Badge>
                </div>
                <p className="roadmap__scope">{r.scope}</p>
                <p className="roadmap__status">
                  <InlineMarkdown text={r.status} repoUrl={site.repoUrl} />
                </p>
              </Reveal>
            ))}
          </ol>
        </section>

        <section className="lcta" aria-labelledby="cta-title">
          <h2 id="cta-title">See a run fail, repair, and get graded.</h2>
          <p>Pipeline graph, attempt-by-attempt diffs, per-unit results and the review gate, all from recorded data.</p>
          <div className="hero__ctas">
            <a className="btn btn--primary btn--lg" href={href('overview')}>
              Open live demo <Icon name="arrowRight" />
            </a>
            <a className="btn btn--secondary btn--lg" href={href('repair')}>
              Jump to repair attempts
            </a>
          </div>
        </section>
      </main>

      <footer className="lfoot">
        <div className="lfoot__inner">
          <Brand />
          <p className="lfoot__text">
            Every number on this site is generated from {data.generatedFrom.length} committed files; a test fails if it drifts
            from them.
          </p>
          <a className="lfoot__link" href={site.repoUrl} target="_blank" rel="noreferrer">
            <Icon name="github" /> Repository
          </a>
        </div>
      </footer>
    </div>
  )
}
