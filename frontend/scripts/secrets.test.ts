// @vitest-environment node
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { scan } from './secrets.ts'

const frontend = join(import.meta.dirname, '..')
const root = join(frontend, '..')

describe('no secrets in what the UI ships', () => {
  it('generated data and fixtures hold no secret-shaped string', () => {
    expect(scan(root, [join(frontend, 'src', 'data'), join(frontend, 'src', 'fixtures')])).toEqual([])
  })

  it('the scanner catches planted tokens (positive control)', () => {
    // Built at run time so no token-shaped string is ever committed.
    const dir = mkdtempSync(join(tmpdir(), 'morph-secrets-'))
    try {
      writeFileSync(join(dir, 'planted.js'), `const a = "gsk_${'x'.repeat(24)}"; const b = "Bearer ${'y'.repeat(24)}"`)
      expect(scan(root, [dir]).map((f) => f.kind)).toEqual(expect.arrayContaining(['Groq key', 'bearer token']))
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})
