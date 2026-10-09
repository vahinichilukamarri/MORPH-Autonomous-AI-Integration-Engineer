import '@fontsource-variable/bricolage-grotesque/wght.css'
import '@fontsource/ibm-plex-sans/latin-400.css'
import '@fontsource/ibm-plex-sans/latin-500.css'
import '@fontsource/ibm-plex-sans/latin-600.css'
import '@fontsource/ibm-plex-mono/latin-400.css'
import '@fontsource/ibm-plex-mono/latin-500.css'
import './styles/tokens.css'
import './styles/base.css'
import './styles/components.css'

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { Root } from './app/Root'

const root = document.getElementById('root')
if (!root) throw new Error('missing #root')

createRoot(root).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)
