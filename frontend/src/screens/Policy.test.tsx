import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import Policy from './Policy'

describe('Policy & audit (preview data)', () => {
  it('labels the screen as Preview and verifies the hash chain', async () => {
    render(<Policy />)
    expect(screen.getAllByText('Preview').length).toBeGreaterThan(0)
    expect(screen.queryByText('FIXTURE')).toBeNull()
    expect(await screen.findByText(/Hash chain verified/)).toBeTruthy()
    expect(screen.getAllByText('DENY').length).toBeGreaterThan(0)
    expect(screen.getAllByText('NEEDS_APPROVAL').length).toBeGreaterThan(0)
  })

  it('records an approval decision in the log and keeps the chain valid', async () => {
    render(<Policy />)
    await screen.findByText(/Hash chain verified/)
    const before = screen.queryAllByText('APPROVAL_DECIDED').length
    const queue = screen.getByRole('region', { name: 'Approval queue' })
    fireEvent.click(within(queue).getAllByRole('button', { name: 'Approve' })[0])
    await waitFor(() => expect(screen.queryAllByText('APPROVAL_DECIDED')).toHaveLength(before + 1))
    expect(await screen.findByText(/Hash chain verified/)).toBeTruthy()
  })
})
