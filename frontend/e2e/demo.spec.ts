/**
 * Demo-mode smoke test against the static build: the landing page loads, its CTA reaches the app,
 * every screen loads with no backend and shows the recorded-run label, the command palette and the
 * shortcuts dialog open, and nothing logs a console error or warning. With MORPH_SCREENSHOTS=1 it also
 * writes the README screenshots to docs/img/ (reduced motion, so every figure is at its final value).
 */
import { join } from 'node:path'

import { expect, test, type Page } from '@playwright/test'

const LABEL = 'Recorded run: Groq openai/gpt-oss-120b, N=1, fixed-start'
const SHOTS = process.env.MORPH_SCREENSHOTS === '1'
const IMG = join(import.meta.dirname, '..', '..', 'docs', 'img')

const SCREENS = [
  { key: '1', route: 'overview', heading: 'An integration engineer that shows its work', ready: '.metrics' },
  { key: '2', route: 'pipeline?unit=support_user_to_crm_customer:L1R:approved', heading: 'Pipeline', ready: '.react-flow__node' },
  { key: '3', route: 'repair', heading: 'Repair attempts', ready: '.monaco-diff-editor .view-line' },
  { key: '4', route: 'results', heading: 'Results', ready: 'td.cell--incorrect' },
  { key: '5', route: 'review', heading: 'Review gate', ready: '.blocked-list' },
  { key: '6', route: 'policy', heading: 'Policy & audit', ready: 'text=Hash chain verified' },
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
  await page.goto(`/#/app/${route}`)
  await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible()
  // The diff editor loads only when its panel nears the viewport; on narrow screens it is below the fold.
  if (ready.includes('monaco')) await page.locator('.repair-grid__diff').scrollIntoViewIfNeeded()
  await expect(page.locator(ready).first()).toBeVisible({ timeout: 20_000 })
  await expect(page.getByText(LABEL, { exact: true })).toBeVisible()
}

async function shot(page: Page, name: string, fullPage = false) {
  if (!SHOTS) return
  await page.waitForLoadState('networkidle')
  await page.screenshot({ path: join(IMG, `ui-${name}.png`), animations: 'disabled', fullPage })
}

test.beforeEach(async ({ page }) => {
  if (SHOTS) await page.emulateMedia({ reducedMotion: 'reduce' })
})

test('the landing page loads and its CTA reaches the app', async ({ page }) => {
  const problems = watch(page)
  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1, name: /connects two APIs/ })).toBeVisible()
  await expect(page.getByText('READY is not correctness.')).toBeVisible()
  await expect(page.getByText(/^N=1 per unit\.$/)).toBeVisible()
  await expect(page.getByRole('link', { name: /View on GitHub/ })).toHaveAttribute('href', /^https:\/\/github\.com\//)
  await shot(page, 'landing')

  await page.getByRole('navigation', { name: 'Sections' }).getByRole('link', { name: 'Results' }).click()
  await expect(page.getByRole('heading', { level: 2, name: /What happened when it ran/ })).toBeInViewport()
  await expect(page).toHaveURL(/\/#?\/?$/)

  await page.getByRole('link', { name: /Open live demo/ }).first().click()
  await expect(page).toHaveURL(/#\/app\/overview$/)
  await expect(page.getByRole('heading', { level: 1, name: SCREENS[0].heading })).toBeVisible()
  await expect(page.getByText(LABEL, { exact: true })).toBeVisible()
  expect(problems).toEqual([])
})

test('every screen loads from static data with no console error', async ({ page }) => {
  const problems = watch(page)
  for (const s of SCREENS) {
    await open(page, s.route, s.ready, s.heading)
    await shot(page, s.route.split('?')[0])
  }
  expect(problems).toEqual([])
})

test('screens are reachable by sidebar, number keys and the command palette', async ({ page }) => {
  const problems = watch(page)
  await open(page, 'overview', '.metrics', SCREENS[0].heading)
  for (const s of SCREENS.slice(1)) {
    await page.keyboard.press(s.key)
    await expect(page.getByRole('heading', { level: 1, name: s.heading })).toBeVisible()
  }
  await page.getByRole('navigation', { name: 'Screens' }).getByRole('link', { name: 'Results' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Results' })).toBeVisible()

  await page.keyboard.press('Control+k')
  const palette = page.getByRole('dialog', { name: 'Command palette' })
  await expect(palette).toBeVisible()
  await page.keyboard.type('repair S1 L2R')
  await shot(page, 'command-palette')
  await page.keyboard.press('Enter')
  await expect(palette).toBeHidden()
  await expect(page.getByRole('heading', { level: 1, name: 'Repair attempts' })).toBeVisible()
  await expect(page.getByText('handed to a person')).toBeVisible()

  await page.keyboard.press('?')
  const help = page.getByRole('dialog', { name: 'Keyboard shortcuts' })
  await expect(help).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(help).toBeHidden()
  expect(problems).toEqual([])
})

test('a pipeline stage opens its detail drawer and a repair attempt can be diffed', async ({ page }) => {
  const problems = watch(page)
  await open(page, SCREENS[1].route, SCREENS[1].ready, 'Pipeline')
  await page.getByRole('button', { name: /^Hidden oracle/ }).click()
  const drawer = page.getByRole('dialog', { name: 'Hidden oracle' })
  await expect(drawer).toBeVisible()
  await expect(drawer.getByText(/checks passed/)).toBeVisible()
  await shot(page, 'pipeline-drawer')
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()

  await open(page, 'repair', '.monaco-diff-editor .view-line', 'Repair attempts')
  await page.getByRole('radio', { name: /S3 L1R/ }).click()
  await page.getByRole('button', { name: /^Attempt 1:/ }).click()
  await expect(page.getByRole('cell', { name: 'SECRET_LITERAL' }).first()).toBeVisible()
  await expect(page.locator('.monaco-diff-editor .view-line').first()).toBeVisible()
  expect(problems).toEqual([])
})

test('the run selector scopes the badge', async ({ page }) => {
  const problems = watch(page)
  await open(page, 'overview', '.metrics', SCREENS[0].heading)
  await page.getByRole('combobox', { name: 'Recorded run' }).selectOption('v0.4')
  await expect(page.getByText('Recorded run: Groq openai/gpt-oss-120b, N=1, one-shot', { exact: true })).toBeVisible()
  await page.keyboard.press('3')
  await expect(page.getByText('No repair attempts in the v0.4 run')).toBeVisible()
  await page.getByRole('button', { name: 'Show the repair run (v0.5)' }).click()
  await expect(page.getByText(LABEL, { exact: true })).toBeVisible()
  expect(problems).toEqual([])
})

test('light theme, tablet and mobile widths', async ({ page }) => {
  const problems = watch(page)
  await open(page, 'results', 'td.cell--incorrect', 'Results')
  await page.getByRole('button', { name: 'Switch to light theme' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await shot(page, 'results-light')
  await page.setViewportSize({ width: 820, height: 1180 })
  for (const s of SCREENS) {
    await open(page, s.route, s.ready, s.heading)
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    expect(overflow, `${s.route} scrolls horizontally at tablet width`).toBeLessThanOrEqual(0)
  }
  await open(page, SCREENS[1].route, SCREENS[1].ready, 'Pipeline')
  await shot(page, 'pipeline-tablet')

  await page.evaluate(() => window.localStorage.removeItem('morph.theme'))
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await expect(page.getByRole('heading', { level: 1, name: /connects two APIs/ })).toBeVisible()
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow, 'the landing page scrolls horizontally at mobile width').toBeLessThanOrEqual(0)
  await page.getByRole('button', { name: 'Sections' }).click()
  await expect(page.getByRole('navigation', { name: 'Sections' }).getByRole('link', { name: 'Roadmap' })).toBeVisible()
  await shot(page, 'landing-mobile')
  expect(problems).toEqual([])
})
