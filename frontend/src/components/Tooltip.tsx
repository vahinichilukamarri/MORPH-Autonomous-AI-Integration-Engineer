import { cloneElement, useId, useState, type ReactElement } from 'react'

/**
 * A text tooltip on hover and keyboard focus, linked with aria-describedby so the hint is read as well
 * as seen. Escape hides it. The trigger must be focusable for keyboard users to reach the hint.
 */
export function Tooltip({ content, children, side = 'top' }: { content: string; children: ReactElement<Record<string, unknown>>; side?: 'top' | 'bottom' }) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const trigger = cloneElement(children, {
    'aria-describedby': id,
    onMouseEnter: () => setOpen(true),
    onMouseLeave: () => setOpen(false),
    onFocus: () => setOpen(true),
    onBlur: () => setOpen(false),
    onKeyDown: (e: { key: string }) => {
      if (e.key === 'Escape') setOpen(false)
    },
  })
  return (
    <span className="tooltip-anchor">
      {trigger}
      <span role="tooltip" id={id} className={`tooltip tooltip--${side}${open ? ' is-open' : ''}`}>
        {content}
      </span>
    </span>
  )
}
