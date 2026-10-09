/** Looks for secret-shaped strings in what the UI ships: generated data, fixtures and the build. */
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const PATTERNS: [string, RegExp][] = [
  ['Groq key', /gsk_[A-Za-z0-9]{16,}/],
  ['OpenAI-style key', /\bsk-[A-Za-z0-9_-]{20,}/],
  ['bearer token', /Bearer\s+[A-Za-z0-9._~+/-]{16,}/],
  ['assigned API key', /(GROQ_API_KEY|MORPH_APPROVER_TOKEN|ADMIN_TOKEN|SUPPORT_TOKEN|CRM_API_KEY)\s*[=:]\s*["']?[A-Za-z0-9_-]{6,}/],
  ['private key', /-----BEGIN [A-Z ]*PRIVATE KEY-----/],
]

/** Non-empty values in the committed .env.example (dev defaults), which must not ship either. */
function exampleValues(root: string): string[] {
  const file = join(root, '.env.example')
  if (!existsSync(file)) return []
  return readFileSync(file, 'utf8')
    .split(/\r?\n/)
    .map((line) => /^\s*[A-Z_]*(KEY|TOKEN|PASSWORD|SECRET)[A-Z_]*=(.+)$/.exec(line)?.[2]?.trim() ?? '')
    .filter((v) => v.length >= 6)
}

function files(path: string): string[] {
  if (!existsSync(path)) return []
  if (statSync(path).isFile()) return [path]
  return readdirSync(path).flatMap((name) => files(join(path, name)))
}

export interface Finding {
  file: string
  kind: string
}

export function scan(root: string, paths: string[]): Finding[] {
  const values = exampleValues(root)
  const findings: Finding[] = []
  for (const file of paths.flatMap(files)) {
    if (!/\.(js|mjs|css|html|json|ts|tsx|map|txt)$/.test(file)) continue
    const text = readFileSync(file, 'utf8')
    for (const [kind, pattern] of PATTERNS) {
      if (pattern.test(text)) findings.push({ file: relative(root, file), kind })
    }
    for (const value of values) {
      if (text.includes(value)) findings.push({ file: relative(root, file), kind: 'value from .env.example' })
    }
  }
  return findings
}
