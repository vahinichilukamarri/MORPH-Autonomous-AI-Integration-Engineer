import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { recorded } from '../data/recorded'
import Repair from './Repair'

// Monaco needs a real browser; the Playwright smoke test covers it. Here the diff is a stub that
// shows what it was given.
vi.mock('./DiffView', () => ({
  default: (p: { original: string; modified: string; language: string }) => (
    <div data-testid="diff-stub">
      {p.language} {p.original.length}→{p.modified.length}
    </div>
  ),
}))

const repairUnits = recorded.units.filter((u) => u.milestone === 'v0.5')
const params = (key: string) => new URLSearchParams({ unit: key })

describe('Repair attempts', () => {
  it('shows the timeline, the terminal state and the verdict of a READY-but-incorrect unit', async () => {
    const unit = repairUnits.find((u) => u.status === 'READY' && u.integrationCorrect === false)
    if (!unit) throw new Error('no READY-but-incorrect unit')
    render(<Repair params={params(unit.key)} />)
    const status = screen.getAllByRole('status').find((el) => el.classList.contains('terminal'))
    if (!status) throw new Error('no terminal banner')
    expect(within(status).getByText('READY')).toBeTruthy()
    expect(within(status).getByText('READY · incorrect')).toBeTruthy()
    const timeline = screen.getByRole('list', { name: 'Attempts' })
    expect(within(timeline).getAllByRole('button')).toHaveLength(unit.attempts.length)
    expect(await screen.findByTestId('diff-stub')).toBeTruthy()
  })

  it('shows the structured feedback of a failed attempt, with file and line marked as not recorded', () => {
    const unit = repairUnits.find((u) => u.attempts.some((a) => a.n > 0 && a.feedback.length > 0))
    if (!unit) throw new Error('no repair attempt with feedback')
    const attempt = unit.attempts.find((a) => a.n > 0 && a.feedback.length > 0)
    if (!attempt) throw new Error('no attempt')
    render(<Repair params={params(unit.key)} />)
    fireEvent.click(screen.getByRole('button', { name: new RegExp(`^Attempt ${attempt.n}\\b`) }))
    const table = screen.getByRole('table')
    for (const f of attempt.feedback) expect(within(table).getAllByText(f.code).length).toBeGreaterThan(0)
    expect(within(table).getAllByText('not recorded').length).toBe(attempt.feedback.length * 2)
  })

  it('a unit that ran out of repairs says so and names the reason', () => {
    const unit = repairUnits.find((u) => u.status === 'HUMAN_REVIEW_REQUIRED')
    if (!unit) throw new Error('no HUMAN_REVIEW_REQUIRED unit')
    render(<Repair params={params(unit.key)} />)
    expect(screen.getByText('handed to a person')).toBeTruthy()
    if (unit.reason) expect(screen.getByText(unit.reason)).toBeTruthy()
  })

  it('guard trips are visible on the attempt that tripped them', async () => {
    const unit = repairUnits.find((u) => u.attempts.some((a) => a.guardEnforced.length > 0))
    if (!unit) throw new Error('no guard trip in the recorded data')
    const attempt = unit.attempts.find((a) => a.guardEnforced.length > 0)
    if (!attempt) throw new Error('no attempt')
    render(<Repair params={params(unit.key)} />)
    fireEvent.click(screen.getByRole('button', { name: new RegExp(`^Attempt ${attempt.n}\\b`) }))
    await waitFor(() => expect(screen.getAllByText(`${attempt.guardEnforced[0]} tripped`).length).toBeGreaterThan(0))
  })
})
