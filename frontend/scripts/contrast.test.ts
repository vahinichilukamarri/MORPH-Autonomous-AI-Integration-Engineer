// @vitest-environment node
/** WCAG AA contrast for every text colour token on every surface, in both themes. */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

const css = readFileSync(join(import.meta.dirname, '..', 'src', 'styles', 'tokens.css'), 'utf8')

function block(selector: string): Record<string, string> {
  const start = css.indexOf(`${selector} {`)
  const body = css.slice(start, css.indexOf('\n}', start))
  return Object.fromEntries([...body.matchAll(/--([\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]))
}

type Rgb = [number, number, number]

function parse(value: string, under?: Rgb): Rgb {
  const hex = /^#([0-9a-f]{6})$/i.exec(value)
  if (hex) return [0, 2, 4].map((i) => parseInt(hex[1].slice(i, i + 2), 16)) as Rgb
  const rgba = /^rgb\((\d+) (\d+) (\d+) \/ ([\d.]+)\)$/.exec(value)
  if (rgba && under) {
    const a = Number(rgba[4])
    return [1, 2, 3].map((i, k) => Number(rgba[i]) * a + under[k] * (1 - a)) as Rgb
  }
  throw new Error(`cannot parse colour ${value}`)
}

function luminance([r, g, b]: Rgb): number {
  const lin = (c: number) => {
    const s = c / 255
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
}

export function ratio(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

const dark = block(':root')
const themes = { dark, light: { ...dark, ...block(":root[data-theme='light']") } }
const TEXT = ['text', 'text-muted', 'text-faint', 'accent', 'ok', 'fail', 'human', 'blocked', 'incorrect', 'skipped', 'fixture']
const SURFACES = ['bg', 'surface-1', 'surface-2', 'surface-3']
const STATUS = ['ok', 'fail', 'human', 'blocked', 'incorrect', 'skipped', 'accent']

describe.each(Object.entries(themes))('%s theme', (_name, t) => {
  it.each(TEXT.flatMap((fg) => SURFACES.map((bg) => [fg, bg])))('%s on %s is at least 4.5:1', (fg, bg) => {
    expect(ratio(parse(t[fg]), parse(t[bg]))).toBeGreaterThanOrEqual(4.5)
  })
  it.each(STATUS)('%s on its tinted badge is at least 4.5:1', (s) => {
    for (const surface of ['surface-1', 'surface-2']) {
      const under = parse(t[surface])
      expect(ratio(parse(t[s]), parse(t[`${s}-bg`], under))).toBeGreaterThanOrEqual(4.5)
    }
  })
  it('button ink on the accent is at least 4.5:1', () => {
    expect(ratio(parse(t['accent-ink']), parse(t.accent))).toBeGreaterThanOrEqual(4.5)
  })
  it('the focus ring is at least 3:1 against every surface', () => {
    for (const bg of SURFACES) expect(ratio(parse(t.focus), parse(t[bg]))).toBeGreaterThanOrEqual(3)
  })
})
