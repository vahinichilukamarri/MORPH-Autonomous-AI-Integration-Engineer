/**
 * Bundle sanity after `npm run build`: Monaco and React Flow must not be in the entry chunk or
 * anything it imports statically, and both must be lazy chunks. Prints measured sizes.
 * Run: npm run check:bundle
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { gzipSync } from 'node:zlib'

interface Chunk {
  file: string
  src?: string
  name?: string
  isEntry?: boolean
  isDynamicEntry?: boolean
  imports?: string[]
  dynamicImports?: string[]
}

const dist = join(dirname(fileURLToPath(import.meta.url)), '..', 'dist')
const manifest = JSON.parse(readFileSync(join(dist, '.vite', 'manifest.json'), 'utf8')) as Record<string, Chunk>

const entryKey = Object.keys(manifest).find((k) => manifest[k].isEntry)
if (!entryKey) throw new Error('no entry chunk in the manifest')

const staticKeys = new Set<string>()
const visit = (key: string) => {
  if (staticKeys.has(key)) return
  staticKeys.add(key)
  for (const next of manifest[key].imports ?? []) visit(next)
}
visit(entryKey)

const HEAVY = [
  { name: 'Monaco', module: 'src/screens/DiffView.tsx', marker: /monaco-diff-editor|MonacoEnvironment/ },
  { name: 'React Flow', module: 'src/screens/PipelineGraph.tsx', marker: /react-flow__renderer|react-flow__viewport/ },
]

const kb = (bytes: number) => `${(bytes / 1024).toFixed(1)} kB`
const size = (file: string) => {
  const buf = readFileSync(join(dist, file))
  return { raw: buf.length, gzip: gzipSync(buf).length }
}

let failed = false
for (const heavy of HEAVY) {
  const chunk = manifest[heavy.module]
  if (!chunk?.isDynamicEntry) {
    console.error(`${heavy.name}: ${heavy.module} is not a lazy chunk`)
    failed = true
    continue
  }
  for (const key of staticKeys) {
    const text = readFileSync(join(dist, manifest[key].file), 'utf8')
    if (key.includes(heavy.module) || heavy.marker.test(text)) {
      console.error(`${heavy.name} code is reachable from the entry through ${manifest[key].file}`)
      failed = true
    }
  }
  const s = size(chunk.file)
  console.log(`${heavy.name} (lazy) ${chunk.file}: ${kb(s.raw)}, gzip ${kb(s.gzip)}`)
}

const initial = [...staticKeys].map((k) => size(manifest[k].file)).reduce(
  (a, b) => ({ raw: a.raw + b.raw, gzip: a.gzip + b.gzip }),
  { raw: 0, gzip: 0 },
)
console.log(`initial JavaScript (entry + static imports, ${staticKeys.size} file(s)): ${kb(initial.raw)}, gzip ${kb(initial.gzip)}`)
if (failed) process.exit(1)
