import '@fontsource/ibm-plex-sans-condensed/400.css'
import '@fontsource/ibm-plex-sans-condensed/500.css'
import '@fontsource/ibm-plex-sans-condensed/600.css'
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/ibm-plex-mono/500.css'
import './styles/tokens.css'
import './styles/base.css'
import './styles/app.css'

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from './app/App'

const root = document.getElementById('root')
if (!root) throw new Error('missing #root')

/** Wait briefly for the two UI faces so the first render does not shift when they swap in. */
function fontsReady(): Promise<unknown> {
  const faces = ['400 1em "IBM Plex Sans Condensed"', '400 1em "IBM Plex Mono"'].map((f) => document.fonts.load(f))
  return Promise.race([Promise.all(faces), new Promise((resolve) => setTimeout(resolve, 600))])
}

void fontsReady()
  .catch(() => undefined)
  .then(() =>
    createRoot(root).render(
      <StrictMode>
        <App />
      </StrictMode>,
    ),
  )
