/**
 * The status bar — the operator's first read.
 *
 * Four facts, and no more. The old console showed ten statistics in this
 * strip, which meant the operator had to work out which two or three
 * mattered before they could read any of them. The four here are the ones
 * that change what an operator does:
 *
 *   - **Overall** — is anything wrong right now?
 *   - **Service level** — is demand being met?
 *   - **Disrupted routes** — is the network intact?
 *   - **Tick** — is the world moving?
 *
 * Everything else the backend reports — uptime, event counts, snapshot age,
 * probe failures — is still available, behind the Diagnostics disclosure.
 * Nothing is thrown away; it just stops competing.
 */

import { useState } from 'react'
import type { BrokerHealth, Health, Metrics, Route, Station } from '../api/types'
import { Badge } from './common'
import { count, duration, percentOfHundred } from '../lib/format'
import {
  closedStations,
  disruptedRoutes,
  serviceLevel as serviceLevelOf,
  worstLevel,
} from '../lib/derive'
import type { Theme } from '../lib/useTheme'

export function StatusBar({
  health,
  broker,
  metrics,
  stations,
  routes,
  theme,
  onToggleTheme,
}: {
  health: Health | null
  broker: BrokerHealth | null
  metrics: Metrics | null
  stations: Station[]
  routes: Route[]
  theme: Theme
  onToggleTheme: () => void
}) {
  const serviceLevel = metrics ? metrics.kpis.service_level_pct / 100 : null
  const serviceState = serviceLevelOf(serviceLevel)

  const disrupted = disruptedRoutes(routes).length
  const closed = closedStations(stations).length

  // The overall verdict is the *worst* of the three alert readings. A muted
  // label must never make a summary look healthy — absence of evidence is
  // not evidence of health.
  const problems = worstLevel([
    serviceState,
    disrupted > 0 ? 'danger' : 'ok',
    closed > 0 ? 'danger' : 'ok',
  ])

  const overallLabel =
    problems === 'ok' ? 'nominal' : problems === 'warn' ? 'degraded' : 'attention'

  return (
    <div className="statusbar">
      <div className="statusbar__brand">
        <h1 className="statusbar__title">Fuel Supply Intelligence</h1>
        <p className="statusbar__subtitle">
          Operator console{metrics ? ` · tick ${metrics.tick}` : ' · connecting'}
        </p>
      </div>

      <div className="statusbar__facts">
        <div className="fact">
          <span className="fact__label">Overall</span>
          <Badge level={problems} pulse={problems !== 'ok'}>
            {overallLabel}
          </Badge>
        </div>

        <div className="fact">
          <span className="fact__label">Service level</span>
          <span className={`fact__value fact__value--${serviceState}`}>
            {metrics ? percentOfHundred(metrics.kpis.service_level_pct) : '—'}
          </span>
        </div>

        <div className="fact">
          <span className="fact__label">Disrupted routes</span>
          <span className={`fact__value fact__value--${disrupted > 0 ? 'danger' : 'ok'}`}>
            {count(disrupted)}
          </span>
        </div>

        <div className="fact">
          <span className="fact__label">Closed stations</span>
          <span className={`fact__value fact__value--${closed > 0 ? 'danger' : 'ok'}`}>
            {count(closed)}
          </span>
        </div>

        <div className="fact">
          <span className="fact__label">Dispatched</span>
          <span className="fact__value fact__value--dim">
            {metrics ? `${metrics.metrics.allocation_liters.toLocaleString()} L` : '—'}
          </span>
        </div>
      </div>

      <div className="statusbar__spacer" />

      <button
        type="button"
        className="btn btn--icon"
        onClick={onToggleTheme}
        aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
        title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
      >
        {theme === 'dark' ? '☀' : '☾'}
      </button>

      {/*
        * The diagnostics disclosure. This is where an operator goes when
        * something is wrong and the headline does not say why — the stream
        * mode, the snapshot age, the probe failures.
        */}
      <DiagnosticsDisclosure health={health} broker={broker} />
    </div>
  )
}

/**
 * The broker's own diagnostics, hidden by default.
 *
 * These figures are genuinely useful during an incident and genuinely
 * irrelevant the rest of the time. That is the definition of a disclosure.
 */
function DiagnosticsDisclosure({
  health,
  broker,
}: {
  health: Health | null
  broker: BrokerHealth | null
}) {
  const [open, setOpen] = useState(false)

  return (
    <div style={{ position: 'relative' }}>
      <button
        type="button"
        className="btn btn--icon"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-label="Diagnostics"
        title="Diagnostics"
      >
        ⋯
      </button>
      {open ? (
        <div
          className="panel"
          style={{
            position: 'absolute',
            right: 0,
            top: 'calc(100% + 8px)',
            width: 300,
            zIndex: 40,
            boxShadow: 'var(--shadow)',
          }}
        >
          <div className="panel__head">
            <h2 className="panel__title">Diagnostics</h2>
          </div>
          <div className="panel__body">
            <div className="kv">
              <span className="kv__key">Broker mode</span>
              <span className="kv__value">{broker?.broker.mode ?? '—'}</span>
            </div>
            <div className="kv">
              <span className="kv__key">SSE reachable</span>
              <span className="kv__value">
                {broker ? String(broker.broker.sse_reachable) : '—'}
              </span>
            </div>
            <div className="kv">
              <span className="kv__key">Suspected drops</span>
              <span className="kv__value">{count(broker?.broker.suspected_drops ?? 0)}</span>
            </div>
            <div className="kv">
              <span className="kv__key">Events received</span>
              <span className="kv__value">{count(broker?.broker.events_received ?? 0)}</span>
            </div>
            <div className="kv">
              <span className="kv__key">Snapshot age</span>
              <span className="kv__value">
                {duration(broker?.snapshot.age_seconds ?? 0)}
              </span>
            </div>
            <div className="kv">
              <span className="kv__key">Uptime</span>
              <span className="kv__value">
                {duration(broker?.broker.uptime_seconds ?? 0)}
              </span>
            </div>
            <div className="kv">
              <span className="kv__key">Dispatches journalled</span>
              <span className="kv__value">{count(health?.journal.total ?? 0)}</span>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
