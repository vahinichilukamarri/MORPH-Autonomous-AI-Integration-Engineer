import { useCallback, useEffect, useState } from 'react'

export type AsyncState<T> =
  | { kind: 'loading' }
  | { kind: 'ok'; data: T }
  | { kind: 'error'; error: Error }

interface Settled<T> {
  key: unknown[]
  state: AsyncState<T>
}

const same = (a: unknown[], b: unknown[]) => a.length === b.length && a.every((v, i) => Object.is(v, b[i]))

/** Runs `load` on mount and whenever `deps` change; `reload` runs it again. A result is shown only
 * for the dependencies it was loaded with, so a change shows `loading` without a state reset. */
export function useAsync<T>(load: (signal: AbortSignal) => Promise<T>, deps: unknown[]): [AsyncState<T>, () => void] {
  const [nonce, setNonce] = useState(0)
  const [settled, setSettled] = useState<Settled<T> | null>(null)
  const key = [...deps, nonce]

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setSettled({ key, state: { kind: 'ok', data } })
      },
      (error: unknown) => {
        if (!controller.signal.aborted) {
          setSettled({ key, state: { kind: 'error', error: error instanceof Error ? error : new Error(String(error)) } })
        }
      },
    )
    return () => controller.abort()
    // The caller owns the dependency list, as with useEffect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, key)

  const reload = useCallback(() => setNonce((n) => n + 1), [])
  const state: AsyncState<T> = settled && same(settled.key, key) ? settled.state : { kind: 'loading' }
  return [state, reload]
}
