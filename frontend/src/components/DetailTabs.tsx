/**
 * The drill-down strip.
 *
 * One tab, one panel. The old console showed nine panels at once, all
 * expanded, so the operator had to scroll past the network to reach the
 * forecast and past the forecast to find the anomalies. Here the panels take
 * turns — and a tab that has something to say says so with a count, so a
 * problem is never hidden behind a tab nobody opens.
 */

import { useState } from 'react'
import type { ReactNode } from 'react'

export interface TabDefinition {
  id: string
  label: string
  /** A count shown on the tab — e.g. how many anomalies are open. */
  count?: number
  /** When true, the count is a problem count and is coloured accordingly. */
  countIsProblem?: boolean
  content: ReactNode
}

export function DetailTabs({ tabs }: { tabs: TabDefinition[] }) {
  const [activeId, setActiveId] = useState(tabs[0]?.id ?? '')

  // The active tab may have been removed (a payload with no anomalies is
  // still a tab, but a defensive fallback keeps the strip from going blank).
  const active = tabs.find((tab) => tab.id === activeId) ?? tabs[0]

  if (!active) return null

  return (
    <div className="tabs">
      <div className="tabs__strip" role="tablist">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={tab.id === active.id}
            className={`tab${tab.id === active.id ? ' tab--active' : ''}`}
            onClick={() => setActiveId(tab.id)}
          >
            {tab.label}
            {tab.count !== undefined && tab.count > 0 ? (
              <span className={`tab__count${tab.countIsProblem ? ' tab__count--danger' : ''}`}>
                {tab.count}
              </span>
            ) : null}
          </button>
        ))}
      </div>

      <div className="tabs__panel" role="tabpanel">
        {active.content}
      </div>
    </div>
  )
}
