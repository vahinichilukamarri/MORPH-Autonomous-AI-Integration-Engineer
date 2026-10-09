import { AnimatePresence } from 'motion/react'
import * as m from 'motion/react-m'
import { useEffect, useId, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

import { DURATION, EASE } from '../motion/tokens'
import { Icon } from './Icon'

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea, [tabindex]:not([tabindex="-1"])'

/**
 * Modal dialog: focus moves in and is trapped, Escape and the backdrop close it, and focus returns to
 * where it was. `variant="drawer"` slides in from the right edge instead of scaling in.
 */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  variant = 'center',
  hideTitle = false,
  initialFocus,
  className = '',
}: {
  open: boolean
  onClose: () => void
  title: string
  description?: ReactNode
  children: ReactNode
  variant?: 'center' | 'drawer'
  hideTitle?: boolean
  initialFocus?: string
  className?: string
}) {
  const panel = useRef<HTMLDivElement>(null)
  const titleId = useId()
  const descId = useId()

  useEffect(() => {
    if (!open) return
    const previous = document.activeElement as HTMLElement | null
    const node = panel.current
    const first = (initialFocus && node?.querySelector<HTMLElement>(initialFocus)) || node?.querySelector<HTMLElement>(FOCUSABLE)
    ;(first ?? node)?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        onClose()
        return
      }
      if (e.key !== 'Tab' || !node) return
      const items = [...node.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el.offsetParent !== null || el === document.activeElement)
      if (items.length === 0) {
        e.preventDefault()
        return
      }
      const firstItem = items[0]
      const last = items[items.length - 1]
      if (e.shiftKey && document.activeElement === firstItem) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        firstItem.focus()
      }
    }
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      previous?.focus?.()
    }
  }, [open, onClose, initialFocus])

  const drawer = variant === 'drawer'
  return createPortal(
    <AnimatePresence>
      {open && (
        <div className={`dialog-root dialog-root--${variant}`}>
          <m.div
            className="dialog-backdrop"
            onMouseDown={onClose}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: DURATION.base }}
          />
          <m.div
            ref={panel}
            className={`dialog dialog--${variant} ${className}`}
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            aria-describedby={description ? descId : undefined}
            tabIndex={-1}
            initial={drawer ? { x: 32, opacity: 0 } : { scale: 0.97, opacity: 0, y: 6 }}
            animate={drawer ? { x: 0, opacity: 1 } : { scale: 1, opacity: 1, y: 0 }}
            exit={drawer ? { x: 32, opacity: 0 } : { scale: 0.98, opacity: 0 }}
            transition={{ duration: DURATION.base, ease: EASE.out }}
          >
            <header className={hideTitle ? 'visually-hidden' : 'dialog__head'}>
              <h2 id={titleId} className="dialog__title">
                {title}
              </h2>
              {!hideTitle && (
                <button type="button" className="btn btn--icon btn--ghost" onClick={onClose} aria-label="Close">
                  <Icon name="close" />
                </button>
              )}
            </header>
            {description && (
              <p id={descId} className="dialog__desc">
                {description}
              </p>
            )}
            {children}
          </m.div>
        </div>
      )}
    </AnimatePresence>,
    document.body,
  )
}
