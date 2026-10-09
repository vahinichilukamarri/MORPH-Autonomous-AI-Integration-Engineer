/** The recorded runs, generated from committed files by scripts/build-demo-data.ts. The same data
 * serves both modes: no REST endpoint exposes evaluation results or repair attempts. */
import generated from './demo.generated.json'
import type { DemoData, RepliesData } from './types'

export const recorded = generated as unknown as DemoData

/** The model replies are large, so they load only when the repair view needs them. */
export function loadReplies(): Promise<RepliesData> {
  return import('./replies.generated.json').then((m) => m.default as unknown as RepliesData)
}
