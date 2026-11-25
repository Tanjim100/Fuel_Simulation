/**
 * A disclosure — a summary line that expands to reveal detail.
 *
 * The console's central editorial decision: **the operator sees the headline;
 * the evidence is one click away.** Nothing is deleted, it is just not
 * shouted. An anomaly's raw metrics, a plan's full risk list, the broker's
 * diagnostics — all still reachable, none of them competing for attention.
 *
 * The disclosure is uncontrolled by default (it manages its own open state),
 * but a caller can drive it when it needs to know whether the section is
 * open — the "Details" button in the hero, for instance, has to change its
 * own label.
 */

import { useId, useState } from 'react'
import type { ReactNode } from 'react'

export function Disclosure({
  label,
  children,
  defaultOpen = false,
  open: controlledOpen,
  onOpenChange,
  openLabel,
}: {
  label: string
  children: ReactNode
  defaultOpen?: boolean
  open?: boolean
  onOpenChange?: (open: boolean) => void
  openLabel?: string
}) {
  const [internalOpen, setInternalOpen] = useState(defaultOpen)
  const contentId = useId()

  // Controlled when `open` is given, uncontrolled otherwise.
  const isControlled = controlledOpen !== undefined
  const isOpen = isControlled ? controlledOpen : internalOpen

  const toggle = () => {
    const next = !isOpen
    if (!isControlled) setInternalOpen(next)
    onOpenChange?.(next)
  }

  return (
    <div className="disclosure">
      <button
        type="button"
        className="disclosure__toggle"
        onClick={toggle}
        aria-expanded={isOpen}
        aria-controls={contentId}
      >
        <span
          className={`disclosure__chevron${isOpen ? ' disclosure__chevron--open' : ''}`}
          aria-hidden="true"
        >
          ▶
        </span>
        {isOpen && openLabel ? openLabel : label}
      </button>
      {isOpen ? (
        <div className="disclosure__body" id={contentId}>
          {children}
        </div>
      ) : null}
    </div>
  )
}
