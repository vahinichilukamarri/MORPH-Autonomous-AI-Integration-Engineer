// @vitest-environment node
/**
 * Repository hygiene for the frontend: the motion tokens in TypeScript match the CSS tokens, and no
 * source, test, config or public file names a coding assistant.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { describe, expect, it } from 'vitest'

import { DURATION, EASE } from '../src/motion/tokens.ts'

const frontend = join(import.meta.dirname, '..')
const css = readFileSync(join(frontend, 'src', 'styles', 'tokens.css'), 'utf8')
const cssVar = (name: string) => new RegExp(String.raw`--${name}:\s*([^;]+);`).exec(css)?.[1].trim()

describe('motion tokens', () => {
  it.each(Object.entries(DURATION))('duration %s matches the CSS token', (name, seconds) => {
    expect(cssVar(`dur-${name}`)).toBe(`${Math.round(seconds * 1000)}ms`)
  })

  it.each([
    ['out', 'ease-out'],
    ['inOut', 'ease-in-out'],
  ] as const)('easing %s matches the CSS token', (name, token) => {
    expect(cssVar(token)).toBe(`cubic-bezier(${EASE[name].join(', ')})`)
  })
})

describe('no coding-assistant mentions', () => {
  // Built from parts so this file does not match itself.
  const names = [['Clau', 'de'], ['Anthro', 'pic'], ['Co', 'pilot'], ['Chat', 'GPT'], ['Co-Authored', '-By']].map((p) => p.join(''))
  const pattern = new RegExp(`\b(${names.join('|')})\b`, 'i')
  const roots = ['src', 'scripts', 'e2e', 'public', 'deploy', 'index.html', 'package.json', 'vercel.json', 'vite.config.ts', 'playwright.config.ts']

  function walk(path: string): string[] {
    if (statSync(path).isFile()) return [path]
    return readdirSync(path).flatMap((name) => walk(join(path, name)))
  }

  const files = roots
    .flatMap((r) => {
      try {
        return walk(join(frontend, r))
      } catch {
        return []
      }
    })
    .filter((f) => /\.(tsx?|css|html|json|svg|txt|md|yml|mjs)$/.test(f))

  it.each(files.map((f) => relative(frontend, f)))('%s', (file) => {
    expect(readFileSync(join(frontend, file), 'utf8')).not.toMatch(pattern)
  })
})
