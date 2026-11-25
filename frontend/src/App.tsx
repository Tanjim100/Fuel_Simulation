/**
 * The operator console.
 *
 * Three columns, left to right:
 *
 *   1. **The network** — depots, stations, routes. What is the world?
 *   2. **The decision** — the plan, the explanation, the dispatch button.
 *   3. **The intelligence** — forecast, anomalies, incidents, journalled
 *      dispatches. What does the world need, and what is wrong with it?
 *
 * The order is deliberate. An operator reads left to right: the physical
 * network, then what to do about it, then the evidence and the history.
 */

import { useMemo } from 'react'
import { AnomalyPanel, IncidentPanel } from './components/AnomalyPanel'
import { DecisionPanel } from './components/DecisionPanel'
import { DemandPanel, ForecastPanel } from './components/ForecastPanel'
import { Badge, ErrorBox, Panel } from './components/common'
import { DepotPanel, RoutePanel, StationPanel } from './components/NetworkPanel'
import { StaleBanner, TelemetryBar } from './components/TelemetryBar'
import { useBackend } from './lib/useBackend'
import { count, duration } from './lib/format'

export default function App() {
  const data = useBackend()

  const risks = useMemo(() => data.plan?.optimization.stockout_risk ?? [], [data.plan])

  const streamBadge = (() => {
    switch (data.streamState) {
      case 'live':
        return <Badge level="ok" pulse>live</Badge>
      case 'polling':
        return <Badge level="warn" pulse>polling</Badge>
      case 'connecting':
        return <Badge level="neutral">connecting</Badge>
      default:
        return <Badge level="danger">offline</Badge>
    }
  })()

  return (
    <div className="app">
      <TelemetryBar
        health={data.health}
        broker={data.broker}
        metrics={data.metrics}
        stations={data.stations}
        routes={data.routes}
      />

      <StaleBanner broker={data.broker} />

      <div style={{ padding: '8px 16px 0' }}>{streamBadge}</div>

      {data.error ? <ErrorBox message={`Backend unreachable — ${data.error}`} /> : null}

      <div className="columns">
        {/* ---------------------------------------------------------- */}
        {/* Column 1 — the network                                     */}
        {/* ---------------------------------------------------------- */}
        <div className="column">
          <DepotPanel depots={data.depots} />
          <StationPanel stations={data.stations} />
          <RoutePanel routes={data.routes} />
        </div>

        {/* ---------------------------------------------------------- */}
        {/* Column 2 — the decision                                    */}
        {/* ---------------------------------------------------------- */}
        <div className="column">
          <DecisionPanel
            plan={data.plan}
            execution={data.execution}
            busy={data.executeBusy || data.planBusy}
            error={data.actionError}
            onExecute={() => void data.execute()}
            onRecompute={() => void data.recompute()}
          />
        </div>

        {/* ---------------------------------------------------------- */}
        {/* Column 3 — the intelligence                                */}
        {/* ---------------------------------------------------------- */}
        <div className="column">
          <ForecastPanel summary={data.forecast} risks={risks} />
          <DemandPanel summary={data.forecast} coverageTicks={data.forecast?.horizon ?? 0} />
          <AnomalyPanel anomalies={data.anomalies} />
          <IncidentPanel incidents={data.incidents} />
          <Panel title="Pipeline">
            <div className="kv">
              <span className="kv__key">Broker mode</span>
              <span className="kv__value">{data.broker?.broker.mode ?? '—'}</span>
            </div>
            <div className="kv">
              <span className="kv__key">SSE reachable</span>
              <span className="kv__value">
                {data.broker ? String(data.broker.broker.sse_reachable) : '—'}
              </span>
            </div>
            <div className="kv">
              <span className="kv__key">Suspected drops</span>
              <span className="kv__value">{count(data.broker?.broker.suspected_drops ?? 0)}</span>
            </div>
            <div className="kv">
              <span className="kv__key">Events received</span>
              <span className="kv__value">{count(data.broker?.broker.events_received ?? 0)}</span>
            </div>
            <div className="kv">
              <span className="kv__key">Snapshot age</span>
              <span className="kv__value">
                {duration(data.broker?.snapshot.age_seconds ?? 0)}
              </span>
            </div>
            <div className="kv">
              <span className="kv__key">Uptime</span>
              <span className="kv__value">{duration(data.broker?.broker.uptime_seconds ?? 0)}</span>
            </div>
            <p className="hint" style={{ marginTop: 12, marginBottom: 0 }}>
              The event stream is advisory: it triggers a re-read, it is never treated as world
              state. If it drops, the console keeps polling and says so.
            </p>
          </Panel>
        </div>
      </div>
    </div>
  )
}
