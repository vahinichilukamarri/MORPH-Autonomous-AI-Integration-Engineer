import { AnimatePresence } from 'motion/react'
import * as m from 'motion/react-m'
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'

import type { Tone } from '../data/derive'
import { DURATION, EASE } from '../motion/tokens'
import { Icon } from './Icon'

interface Toast {
  id: number
  message: ReactNode
  tone: Tone
}

type Notify = (message: ReactNode, tone?: Tone) => void

const ToastContext = createContext<Notify>(() => {})

/** Short confirmations in a polite live region, dismissed after a few seconds or by the close button. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const next = useRef(1)

  const dismiss = useCallback((id: number) => setToasts((list) => list.filter((t) => t.id !== id)), [])
  const notify = useCallback<Notify>(
    (message, tone = 'accent') => {
      const id = next.current++
      setToasts((list) => [...list.slice(-2), { id, message, tone }])
      window.setTimeout(() => dismiss(id), 5000)
    },
    [dismiss],
  )
  const value = useMemo(() => notify, [notify])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        <AnimatePresence initial={false}>
          {toasts.map((t) => (
            <m.div
              key={t.id}
              layout
              className={`toast toast--${t.tone}`}
              initial={{ opacity: 0, y: 12, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 6 }}
              transition={{ duration: DURATION.base, ease: EASE.out }}
            >
              <span className="toast__bar" aria-hidden="true" />
              <span className="toast__msg">{t.message}</span>
              <button type="button" className="btn btn--icon btn--ghost btn--sm" aria-label="Dismiss" onClick={() => dismiss(t.id)}>
                <Icon name="close" size={14} />
              </button>
            </m.div>
          ))}
        </AnimatePresence>
      </div>
    </ToastContext.Provider>
  )
}

export const useToast = (): Notify => useContext(ToastContext)
