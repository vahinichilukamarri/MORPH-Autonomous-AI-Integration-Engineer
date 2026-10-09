import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { recordedLabel, runsPerUnit, summarise } from '../data/derive'
import { recorded } from '../data/recorded'
import { Root } from './Root'
import { setRun } from './runs'

vi.mock('../screens/PipelineGraph', () => ({ default: () => <div data-testid="graph-stub" /> }))
vi.mock('../screens/DiffView', () => ({ default: () => <div data-testid="diff-stub" /> }))

const LABEL = /^Recorded run: Groq openai\/gpt-oss-120b, N=1, fixed-start$/

function at(hash: string) {
  window.location.hash = hash
}

describe('App shell', () => {
  beforeEach(() => {
    setRun('v0.5')
    document.documentElement.dataset.theme = 'dark'
  })

  it('shows the recorded-run label on every screen and moves between screens by number key', async () => {
    at('#/app/overview')
    render(<Root />)
    expect(await screen.findByText(LABEL)).toBeTruthy()
    for (const [key, title] of [
      ['2', 'Pipeline'],
      ['3', 'Repair attempts'],
      ['4', 'Results'],
      ['5', 'Review gate'],
      ['6', 'Policy & audit'],
      ['1', 'An integration engineer that shows its work'],
    ]) {
      act(() => {
        fireEvent.keyDown(window, { key })
      })
      expect(await screen.findByRole('heading', { level: 1, name: title })).toBeTruthy()
      expect(screen.getByText(LABEL)).toBeTruthy()
    }
  })

  it('opens the command palette with Ctrl+K and runs a command', async () => {
    at('#/app/overview')
    render(<Root />)
    await screen.findByText(LABEL)
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    const input = await screen.findByRole('combobox', { name: 'Search commands' })
    fireEvent.change(input, { target: { value: 'go results' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(window.location.hash).toBe('#/app/results')
    expect(await screen.findByRole('heading', { level: 1, name: 'Results' })).toBeTruthy()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('closes the palette on Escape and opens the shortcuts help with ?', async () => {
    at('#/app/overview')
    render(<Root />)
    await screen.findByText(LABEL)
    fireEvent.keyDown(window, { key: 'k', metaKey: true })
    fireEvent.keyDown(await screen.findByRole('combobox', { name: 'Search commands' }), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    fireEvent.keyDown(window, { key: '?' })
    const help = await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })
    expect(within(help).getByText('Open the command palette')).toBeTruthy()
  })

  it('switches between dark and light theme', async () => {
    at('#/app/results')
    render(<Root />)
    fireEvent.click(await screen.findByRole('button', { name: 'Switch to light theme' }))
    expect(document.documentElement.dataset.theme).toBe('light')
    fireEvent.click(await screen.findByRole('button', { name: 'Switch to dark theme' }))
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('the run selector changes the badge to the chosen run', async () => {
    at('#/app/overview')
    render(<Root />)
    const select = await screen.findByRole('combobox', { name: 'Recorded run' })
    fireEvent.change(select, { target: { value: 'v0.4' } })
    expect(await screen.findByText(recordedLabel(recorded, 'v0.4'))).toBeTruthy()
  })

  it('the repair screen offers the v0.5 run when v0.4 is selected', async () => {
    setRun('v0.4')
    at('#/app/repair')
    render(<Root />)
    expect(await screen.findByText('No repair attempts in the v0.4 run')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Show the repair run (v0.5)' }))
    expect(await screen.findByRole('list', { name: 'Attempts' })).toBeTruthy()
  })

  it('old links without /app still open the screen', async () => {
    at('#/results')
    render(<Root />)
    expect(await screen.findByRole('heading', { level: 1, name: 'Results' })).toBeTruthy()
  })
})

describe('Landing page', () => {
  it('states the value, links the demo and GitHub, and shows the generated results with their caveats', async () => {
    at('#/')
    render(<Root />)
    expect(await screen.findByRole('heading', { level: 1, name: /connects two APIs/ })).toBeTruthy()
    const demo = screen.getAllByRole('link', { name: /Open live demo/ })
    expect(demo.length).toBeGreaterThan(0)
    for (const link of demo) expect(link.getAttribute('href')).toBe('#/app/overview')
    expect(screen.getByRole('link', { name: /View on GitHub/ }).getAttribute('href')).toBe(recorded.site.repoUrl)

    const results = screen.getByRole('region', { name: /What happened when it ran/ })
    for (const c of ['D', 'L1', 'L2', 'L1R', 'L2R'] as const) {
      const s = summarise(recorded, c)
      const card = within(results).getByText(c).closest('article')
      if (!card) throw new Error(`no card for ${c}`)
      expect(within(card).getByText(`/${s.units}`)).toBeTruthy()
      expect(within(card).getAllByText(String(s.ready)).length).toBeGreaterThan(0)
    }
    expect(within(results).getByText(`N=${runsPerUnit(recorded)} per unit.`)).toBeTruthy()
    expect(within(results).getByText('READY is not correctness.')).toBeTruthy()
    expect(within(results).getAllByText('READY but incorrect')).toHaveLength(summarise(recorded, 'L1R').readyIncorrect > 0 ? 1 : 0)
  })

  it('lists every guarantee and roadmap item from the README, and marks the policy section Preview', async () => {
    at('#/')
    render(<Root />)
    await screen.findByRole('heading', { level: 1 })
    const guarantees = screen.getByRole('region', { name: /Rules enforced in code/ })
    for (const g of recorded.site.guarantees) expect(within(guarantees).getByRole('heading', { level: 3, name: g.title })).toBeTruthy()
    for (const r of recorded.site.roadmap) expect(screen.getByText(r.tag)).toBeTruthy()
    const policy = screen.getByRole('region', { name: /every tool call gated/ })
    expect(within(policy).getAllByText('Preview').length).toBeGreaterThan(0)
  })
})
