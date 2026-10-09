import { act, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { App } from './App'

vi.mock('../screens/PipelineGraph', () => ({ default: () => <div data-testid="graph-stub" /> }))
vi.mock('../screens/DiffView', () => ({ default: () => <div data-testid="diff-stub" /> }))

describe('App shell', () => {
  it('shows the recorded-run label on every screen', async () => {
    render(<App />)
    const label = screen.getByText(/^Recorded run: Groq openai\/gpt-oss-120b, N=1, fixed-start$/)
    for (const key of ['2', '3', '4', '5', '6', '1']) {
      act(() => {
        fireEvent.keyDown(window, { key })
      })
      await screen.findByRole('heading', { level: 1 })
      expect(label.isConnected).toBe(true)
    }
  })

  it('opens the command palette with Ctrl+K and runs a command', async () => {
    render(<App />)
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: 'go results' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(window.location.hash).toBe('#/results')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(await screen.findByRole('heading', { level: 1, name: 'Results' })).toBeTruthy()
  })

  it('closes the palette on Escape', () => {
    render(<App />)
    fireEvent.keyDown(window, { key: 'k', metaKey: true })
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('switches between dark and light theme', () => {
    render(<App />)
    expect(document.documentElement.dataset.theme).toBe('dark')
    fireEvent.click(screen.getByRole('button', { name: 'Switch to light theme' }))
    expect(document.documentElement.dataset.theme).toBe('light')
    fireEvent.click(screen.getByRole('button', { name: 'Switch to dark theme' }))
    expect(document.documentElement.dataset.theme).toBe('dark')
  })
})
