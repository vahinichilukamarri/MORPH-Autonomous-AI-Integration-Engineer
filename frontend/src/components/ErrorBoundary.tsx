import { Component, type ErrorInfo, type ReactNode } from 'react'

import { ErrorState } from './ui'

interface State {
  error: Error | null
}

/** Keeps a failing screen (for example a chunk that did not load) from blanking the whole app. */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  override state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error(error, info.componentStack)
  }

  override render() {
    if (this.state.error) {
      return (
        <div className="page">
          <ErrorState error={this.state.error} onRetry={() => this.setState({ error: null })} />
        </div>
      )
    }
    return this.props.children
  }
}
