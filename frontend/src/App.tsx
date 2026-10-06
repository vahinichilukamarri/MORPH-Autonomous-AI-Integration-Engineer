import { useEffect, useState } from 'react'

type Health = { status: string; database: string }
type State = { kind: 'loading' } | { kind: 'ok'; health: Health } | { kind: 'error' }

export default function App() {
  const [state, setState] = useState<State>({ kind: 'loading' })

  useEffect(() => {
    const controller = new AbortController()
    fetch('/api/health', { signal: controller.signal })
      .then((r) => r.json() as Promise<Health>)
      .then((health) => setState({ kind: 'ok', health }))
      .catch((e: unknown) => {
        if (!(e instanceof DOMException && e.name === 'AbortError')) setState({ kind: 'error' })
      })
    return () => controller.abort()
  }, [])

  return (
    <main>
      <h1>MORPH</h1>
      <p>
        Backend:{' '}
        {state.kind === 'loading' && 'checking...'}
        {state.kind === 'error' && 'unreachable'}
        {state.kind === 'ok' && `${state.health.status} (database: ${state.health.database})`}
      </p>
    </main>
  )
}
