/** Fails if a secret-shaped string is in the generated data, the fixtures or the build. Run after
 * `npm run build`: npm run check:secrets */
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { scan } from './secrets.ts'

const frontend = join(dirname(fileURLToPath(import.meta.url)), '..')
const root = join(frontend, '..')
const scanned = ['src/data', 'src/fixtures', 'dist'].map((p) => join(frontend, p))
const findings = scan(root, scanned)
for (const f of findings) console.error(`secret-shaped string (${f.kind}) in ${f.file}`)
if (findings.length) process.exit(1)
console.log('no secret-shaped strings in src/data, src/fixtures or dist')
