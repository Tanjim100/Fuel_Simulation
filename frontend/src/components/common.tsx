/**
 * Small shared presentational pieces.
 *
 * Everything here is a pure function of its props — no fetching, no state. That
 * keeps the panels easy to test in isolation and keeps the data flow visible.
 */

import type { ReactNode } from 'react'
import type { HealthLevel } from '../lib/derive'

export function Panel({
  title,
  action,
  flush,
  children,
}: {
  title: string
  action?: ReactNode
  flush?: boolean
  children: ReactNode
}) {
  return (
    <section className="panel">
      <header className="panel__head">
        <h2 className="panel__title">{title}</h2>
        {action}
      </header>
      <div className={flush ? 'panel__body panel__body--flush' : 'panel__body'}>
        {children}
      </div>
    </section>
  )
}

export function Badge({
  level = 'neutral',
  children,
  pulse,
}: {
  level?: HealthLevel | 'accent'
  children: ReactNode
  pulse?: boolean
}) {
  return (
    <span className={`badge badge--${level}`}>
      <span className={pulse ? 'dot dot--pulse' : 'dot'} />
      {children}
    </span>
  )
}

export function Stat({
  label,
  value,
  sub,
  level = 'dim',
}: {
  label: string
  value: string
  sub?: string
  level?: HealthLevel
}) {
  return (
    <div className="stat">
      <span className="stat__label">{label}</span>
      <span className={`stat__value stat__value--${level}`}>{value}</span>
      {sub ? <span className="stat__sub">{sub}</span> : null}
    </div>
  )
}

export function Meter({ ratio, level }: { ratio: number; level: HealthLevel }) {
  const pct = Math.min(100, Math.max(0, ratio * 100))
  return (
    <div
      className="meter"
      role="meter"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div className={`meter__fill meter__fill--${level}`} style={{ width: `${pct}%` }} />
    </div>
  )
}

export function Empty({
  title,
  children,
}: {
  title: string
  children?: ReactNode
}) {
  return (
    <div className="empty">
      <div className="empty__title">{title}</div>
      {children}
    </div>
  )
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div className="error-box" role="alert">
      {message}
    </div>
  )
}

export function KeyValue({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="kv">
      <span className="kv__key">{label}</span>
      <span className="kv__value">{value}</span>
    </div>
  )
}

/**
 * The declared figures behind an explanation.
 *
 * The backend guarantees that every number quoted in the prose also appears in
 * `numbers`. Showing that map verbatim is therefore an *audit*: a reader can
 * check the sentence against the figures, and the console never displays a
 * figure the backend did not declare.
 */
export function NumberChips({ numbers }: { numbers: Record<string, number> }) {
  const entries = Object.entries(numbers)
  if (entries.length === 0) return null
  return (
    <div className="explanation__numbers">
      {entries.map(([key, value]) => (
        <span className="number-chip" key={key}>
          <span className="number-chip__key">{key}</span> {value}
        </span>
      ))}
    </div>
  )
}
