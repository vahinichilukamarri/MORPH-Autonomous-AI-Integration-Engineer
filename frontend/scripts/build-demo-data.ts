/** Writes the demo-mode data under src/data/ from committed files. Run: npm run data */
import { writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { buildDemoData } from './demo-data.ts'

const frontend = join(dirname(fileURLToPath(import.meta.url)), '..')
const { demo, replies } = buildDemoData(join(frontend, '..'))
for (const [name, data] of [
  ['demo.generated.json', demo],
  ['replies.generated.json', replies],
] as const) {
  writeFileSync(join(frontend, 'src', 'data', name), `${JSON.stringify(data, null, 2)}\n`)
  console.log(`wrote src/data/${name}`)
}
