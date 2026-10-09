/** Monaco diff editor, loaded only with the repair view. Local ESM build with the Python and
 * JavaScript grammars only; nothing is fetched from a CDN. */
import 'monaco-editor/languages/definitions/javascript/register'
import 'monaco-editor/languages/definitions/python/register'

import * as monaco from 'monaco-editor/editor'
import EditorWorker from 'monaco-editor/editor/editor.worker?worker'
import { useEffect, useRef } from 'react'

import type { Theme } from '../app/theme'

self.MonacoEnvironment = { getWorker: () => new EditorWorker() }

function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

/** Monaco accepts only #rrggbb; the CSS minifier may have shortened a token to #rgb. */
function hex(name: string): string {
  const value = css(name)
  const short = /^#([0-9a-f])([0-9a-f])([0-9a-f])$/i.exec(value)
  return short ? `#${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}` : value
}

function defineTheme(theme: Theme): string {
  const id = `morph-${theme}`
  monaco.editor.defineTheme(id, {
    base: theme === 'dark' ? 'vs-dark' : 'vs',
    inherit: true,
    rules: [],
    colors: {
      'editor.background': hex('--surface-1'),
      'editor.foreground': hex('--text'),
      'editorLineNumber.foreground': hex('--text-faint'),
      'editorGutter.background': hex('--surface-1'),
    },
  })
  return id
}

export default function DiffView({
  original,
  modified,
  language,
  theme,
  label,
}: {
  original: string
  modified: string
  language: 'python' | 'javascript'
  theme: Theme
  label: string
}) {
  const host = useRef<HTMLDivElement>(null)
  const editor = useRef<monaco.editor.IStandaloneDiffEditor | null>(null)

  useEffect(() => {
    if (!host.current) return
    const instance = monaco.editor.createDiffEditor(host.current, {
      readOnly: true,
      originalEditable: false,
      automaticLayout: true,
      renderSideBySide: host.current.clientWidth > 900,
      useInlineViewWhenSpaceIsLimited: true,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      renderOverviewRuler: false,
      fontFamily: css('--font-mono'),
      fontSize: 12,
      lineHeight: 18,
      wordWrap: 'on',
      ariaLabel: `${label}, new attempt`,
      originalAriaLabel: `${label}, previous attempt`,
    })
    editor.current = instance
    // Monaco's EditContext input does not carry the aria-label options; name both inputs here.
    const name = () => {
      const inputs = host.current?.querySelectorAll<HTMLElement>('.native-edit-context') ?? []
      inputs.forEach((el, i) => el.setAttribute('aria-label', `${label}, ${i === 0 ? 'previous' : 'new'} attempt`))
    }
    const observer = new MutationObserver(name)
    observer.observe(host.current, { childList: true, subtree: true })
    name()
    return () => {
      observer.disconnect()
      const model = instance.getModel()
      instance.dispose()
      model?.original.dispose()
      model?.modified.dispose()
      editor.current = null
    }
  }, [label])

  useEffect(() => {
    const instance = editor.current
    if (!instance) return
    const previous = instance.getModel()
    instance.setModel({
      original: monaco.editor.createModel(original, language),
      modified: monaco.editor.createModel(modified, language),
    })
    previous?.original.dispose()
    previous?.modified.dispose()
  }, [original, modified, language, label])

  useEffect(() => {
    monaco.editor.setTheme(defineTheme(theme))
  }, [theme])

  return <div ref={host} className="diff" data-testid="diff-editor" />
}
