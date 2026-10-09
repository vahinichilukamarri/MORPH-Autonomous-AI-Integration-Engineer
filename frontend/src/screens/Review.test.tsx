import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { recorded } from '../data/recorded'
import Review from './Review'

describe('Review gate', () => {
  it('lists every blocking field of the as-proposed inputs', () => {
    render(<Review />)
    const reasons = recorded.units
      .filter((u) => u.inputSet === 'as_proposed' && u.condition === 'D')
      .flatMap((u) => u.blockedReasons)
    expect(reasons.length).toBeGreaterThan(0)
    for (const r of reasons) {
      expect(screen.getAllByText(r.slice(0, r.lastIndexOf(':'))).length).toBeGreaterThan(0)
    }
    expect(screen.getAllByText('BLOCKED_PENDING_REVIEW').length).toBeGreaterThan(0)
  })
})
