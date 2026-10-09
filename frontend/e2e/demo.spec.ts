/**
 * Demo-mode smoke test against the static build: every screen loads with no backend, shows the
 * recorded-run label and logs no console error. With MORPH_SCREENSHOTS=1 it also writes the README
 * screenshots to docs/img/.
 */
import { join } from 'node:path'

import { expect, test, type Page } from '@playwright/test'

const LABEL = 'Recorded run: Groq openai/gpt-oss-120b, N=1, fixed-start'
const SHOTS = process.env.MORPH_SCREENSHOTS === '1'
const IMG = join(import.meta.dirname, '..', '..', 'docs', 'img')

const SCREENS = [
  { route: 'overview', heading: 'An integration engineer that shows its work', ready: '.metrics' },
  { route: 'pipeline?unit=support_user_to_crm_customer:L1R:approved', heading: 'Pipeline', ready: '.react-flow__node' },
  { route: 'repair', heading: 'Repair attempts', ready: '.monaco-diff-editor .view-line' },
  { route: 'results', heading: 'Results', ready: 'td.cell--incorrect' },
  { route: 'review', heading: 'Review gate', ready: '.blocked-list' },
  { route: 'policy', heading: 'Policy & audit', ready: 'text=Hash chain verified' },
]

function watch(page: Page): string[] {
  const problems: string[] = []
  page.on('console', (m) => {
    if (m.type() === 'error' || m.type() === 'warning') problems.push(`console ${m.type()}: ${m.text()}`)
  })
  page.on('pageerror', (e) => problems.push(`page error: ${e.message}`))
  page.on('requestfailed', (r) => problems.push(`request failed: ${r.url()} ${r.failure()?.errorText ?? ''}`))
  page.on('response', (r) => {
    if (r.status() >= 400) problems.push(`HTTP ${r.status()}: ${r.url()}`)
  })
  return problems
}

async function open(page: Page, route: string, ready: string, heading: string) {
  await page.goto(`/#/${route}`)
  await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible()
  await expect(page.locator(ready).first()).toBeVisible({ timeout: 20_000 })
  await expect(page.getByText(LABEL, { exact: true })).toBeVisible()
}

test('every screen loads from static data with no console error', async ({ page }) => {
  const problems = watch(page)
  for (const s of SCREENS) {
    await open(page, s.route, s.ready, s.heading)
    if (SHOTS) {
      await page.waitForLoadState('networkidle')
      await page.screenshot({ path: join(IMG, `ui-${s.route.split('?')[0]}.png`), animations: 'disabled' })
    }
  }
  expect(problems).toEqual([])
})

test('navigation works from the keyboard and the command palette', async ({ page }) => {
  const problems = watch(page)
  await open(page, 'overview', '.metrics', SCREENS[0].heading)
  await page.keyboard.press('4')
  await expect(page.getByRole('heading', { level: 1, name: 'Results' })).toBeVisible()
  await page.keyboard.press('Control+k')
  const palette = page.getByRole('dialog', { name: 'Command palette' })
  await expect(palette).toBeVisible()
  await page.keyboard.type('repair S1 L2R')
  if (SHOTS) await page.screenshot({ path: join(IMG, 'ui-command-palette.png'), animations: 'disabled' })
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { level: 1, name: 'Repair attempts' })).toBeVisible()
  await expect(page.getByText('handed to a person')).toBeVisible()
  expect(problems).toEqual([])
})

test('a repair attempt can be inspected and diffed', async ({ page }) => {
  const problems = watch(page)
  await open(page, 'repair', '.monaco-diff-editor .view-line', 'Repair attempts')
  await page.getByRole('radio', { name: /S3 L1R/ }).click()
  await page.getByRole('button', { name: /^Attempt 1:/ }).click()
  await expect(page.getByRole('cell', { name: 'SECRET_LITERAL' }).first()).toBeVisible()
  await expect(page.locator('.monaco-diff-editor .view-line').first()).toBeVisible()
  expect(problems).toEqual([])
})

test('light theme and tablet width', async ({ page }) => {
  const problems = watch(page)
  await open(page, 'results', 'td.cell--incorrect', 'Results')
  await page.getByRole('button', { name: 'Switch to light theme' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  if (SHOTS) await page.screenshot({ path: join(IMG, 'ui-results-light.png'), animations: 'disabled' })
  await page.setViewportSize({ width: 820, height: 1180 })
  for (const s of SCREENS) {
    await open(page, s.route, s.ready, s.heading)
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    expect(overflow, `${s.route} scrolls horizontally at tablet width`).toBeLessThanOrEqual(0)
  }
  if (SHOTS) {
    await open(page, 'pipeline', '.react-flow__node', 'Pipeline')
    await page.screenshot({ path: join(IMG, 'ui-pipeline-tablet.png'), animations: 'disabled', fullPage: false })
  }
  expect(problems).toEqual([])
})
