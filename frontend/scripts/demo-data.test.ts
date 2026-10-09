// @vitest-environment node
/**
 * The demo data must be exactly what the generator builds from the committed files. If a result,
 * replay or provenance file changes, this fails until `npm run data` is run and the output reviewed.
 */
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { buildDemoData, SOURCES } from './demo-data.ts'

const frontend = join(import.meta.dirname, '..')
const root = join(frontend, '..')
const committed = (name: string) => JSON.parse(readFileSync(join(frontend, 'src', 'data', name), 'utf8')) as unknown

describe('demo data drift', () => {
  const built = buildDemoData(root)

  it('demo.generated.json equals a rebuild from the committed sources', () => {
    expect(committed('demo.generated.json')).toEqual(JSON.parse(JSON.stringify(built.demo)))
  })

  it('replies.generated.json equals a rebuild from the committed replays', () => {
    expect(committed('replies.generated.json')).toEqual(JSON.parse(JSON.stringify(built.replies)))
  })

  it('the repair results hash matches the hash in the run provenance', () => {
    const provenance = JSON.parse(readFileSync(join(root, SOURCES.repairProvenance), 'utf8')) as {
      files: Record<string, { sha256: string }>
    }
    const used = built.demo.generatedFrom.find((f) => f.path === SOURCES.repairResults)
    expect(used?.sha256).toBe(provenance.files['results.jsonl'].sha256)
  })

  it('every attempt has its recorded reply', () => {
    const ids = built.demo.units.flatMap((u) => u.attempts.map((a) => a.replyId))
    expect(ids.length).toBeGreaterThan(0)
    for (const id of ids) expect(built.replies.replies[id]).toBeDefined()
  })
})

describe('screens carry no hand-typed figures', () => {
  // Counts, ratios and token figures must come from the data. Allowed: digits in identifiers and
  // names (S1, O1, L1R, v0.4, G5), CSS and layout values.
  const dir = join(frontend, 'src', 'screens')
  const files = readdirSync(dir).filter((f) => f.endsWith('.tsx') && !f.endsWith('.test.tsx'))

  it.each(files)('%s', (file) => {
    const source = readFileSync(join(dir, file), 'utf8')
    const prose = [...source.matchAll(/>([^<>{}]*)</g)].map((m) => m[1]).join('\n')
    expect(prose).not.toMatch(/\b\d+\s*\/\s*\d+\b/)
    expect(prose).not.toMatch(/\b\d[\d,]*\s+(calls?|tokens?|checks?|units?|repairs?|attempts?|runs?)\b/i)
    expect(prose).not.toMatch(/\b\d+(\.\d+)?\s*%/)
  })
})
