import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { summarise } from '../data/derive'
import { recorded } from '../data/recorded'
import Results from './Results'

describe('Results', () => {
  it('marks READY-but-incorrect units distinctly, never as a pass', () => {
    const { container } = render(<Results />)
    const expected = summarise(recorded, 'L1R').readyIncorrect + summarise(recorded, 'L2R').readyIncorrect
    expect(expected).toBeGreaterThan(0)
    const cells = container.querySelectorAll<HTMLElement>('td.cell--incorrect')
    expect(cells).toHaveLength(expected)
    for (const cell of cells) {
      expect(within(cell).getByText('READY · incorrect')).toBeTruthy()
      expect(cell.querySelector('.badge--incorrect')).not.toBeNull()
      expect(cell.querySelector('.badge--ok')).toBeNull()
    }
  })

  it('states N per unit and that nothing is significant', () => {
    render(<Results />)
    expect(screen.getByText(/N=1 per unit\./)).toBeTruthy()
    expect(screen.getAllByText(/statistically significant/).length).toBeGreaterThan(0)
  })

  it('shows every condition and every exposure tag', () => {
    render(<Results />)
    for (const c of ['D', 'L1', 'L2', 'L1R', 'L2R']) expect(screen.getAllByText(c).length).toBeGreaterThan(0)
    const tags = new Set(recorded.units.flatMap((u) => (u.exposure ? [u.exposure] : [])))
    expect(tags.size).toBeGreaterThan(0)
    for (const tag of tags) expect(screen.getAllByText(tag).length).toBeGreaterThan(0)
  })
})
