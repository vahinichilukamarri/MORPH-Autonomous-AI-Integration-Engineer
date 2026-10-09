import { useEffect, useId, useMemo, useState } from 'react'

import { Dialog } from '../components/Dialog'
import { Icon } from '../components/Icon'
import { Kbd } from '../components/ui'

export interface Command {
  id: string
  label: string
  group: string
  hint?: string
  run: () => void
}

function matches(command: Command, query: string): boolean {
  const haystack = `${command.group} ${command.label} ${command.hint ?? ''}`.toLowerCase()
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((word) => haystack.includes(word))
}

/** Ctrl/Cmd+K palette: type to filter, arrows to move, Enter to run, Escape to close. */
export function CommandPalette({ open, commands, onClose }: { open: boolean; commands: Command[]; onClose: () => void }) {
  return (
    <Dialog open={open} onClose={onClose} title="Command palette" hideTitle className="palette" initialFocus="input">
      <PaletteBody commands={commands} onClose={onClose} />
    </Dialog>
  )
}

function PaletteBody({ commands, onClose }: { commands: Command[]; onClose: () => void }) {
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const listId = useId()
  const results = useMemo(() => commands.filter((c) => matches(c, query)), [commands, query])
  const current = Math.min(active, Math.max(results.length - 1, 0))

  useEffect(() => {
    document.getElementById(`${listId}-${current}`)?.scrollIntoView?.({ block: 'nearest' })
  }, [current, listId])

  const run = (command: Command | undefined) => {
    if (!command) return
    onClose()
    command.run()
  }

  return (
    <>
      <div className="palette__search">
        <Icon name="search" />
        <input
          className="palette__input"
          placeholder="Jump to a screen, a run, a unit or a setting"
          value={query}
          role="combobox"
          aria-label="Search commands"
          aria-expanded="true"
          aria-controls={listId}
          aria-activedescendant={results.length ? `${listId}-${current}` : undefined}
          aria-autocomplete="list"
          onChange={(e) => {
            setQuery(e.target.value)
            setActive(0)
          }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') {
              e.preventDefault()
              setActive((current + 1) % Math.max(results.length, 1))
            } else if (e.key === 'ArrowUp') {
              e.preventDefault()
              setActive((current - 1 + results.length) % Math.max(results.length, 1))
            } else if (e.key === 'Enter') {
              e.preventDefault()
              run(results[current])
            } else if (e.key === 'Tab') {
              e.preventDefault()
            }
          }}
        />
        <Kbd>Esc</Kbd>
      </div>
      <ul className="palette__list" id={listId} role="listbox" aria-label="Commands">
        {results.length === 0 && <li className="palette__empty">No command matches “{query}”.</li>}
        {results.map((c, i) => {
          const header = i === 0 || results[i - 1].group !== c.group
          return (
            <li
              key={c.id}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === current}
              className={`palette__item${header ? ' palette__item--first' : ''}`}
              data-group={header ? c.group : undefined}
              onMouseMove={() => setActive(i)}
              onClick={() => run(c)}
            >
              <span className="palette__group">{c.group}</span>
              <span className="palette__label">{c.label}</span>
              {c.hint && <span className="palette__hint">{c.hint}</span>}
            </li>
          )
        })}
      </ul>
      <footer className="palette__foot">
        <span>
          <Kbd>↑</Kbd> <Kbd>↓</Kbd> move
        </span>
        <span>
          <Kbd>Enter</Kbd> run
        </span>
        <span>
          <Kbd>Esc</Kbd> close
        </span>
      </footer>
    </>
  )
}
