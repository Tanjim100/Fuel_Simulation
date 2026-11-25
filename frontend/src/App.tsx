/**
 * The operator console.
 *
 * One column, read top to bottom:
 *
 *   1. **Status** — four facts. Is anything wrong right now?
 *   2. **The action** — what to do, why, and the one button that does it.
 *   3. **The drill-down** — stations, forecast and problems, one at a time.
 *
 * The old console was three columns of nine expanded panels. Everything was
 * visible, which meant nothing was. The operator's question — *what should I
 * do, and is it safe?* — was answered in a corner of a wall of tables.
 *
 * The three load-bearing properties are unchanged:
 *
 *   - Every number on screen is a number the backend sent, or a documented
 *     pure derivation of one.
 *   - The event stream is advisory: it triggers a re-read, it is never
 *     treated as world state.
 *   - A dispatch requires an explicit second click.
 */

import { useMemo } from 'react'
import { ActionCard } from './components/ActionCard'
import { AnomalyPanel, IncidentPanel } from './components/AnomalyPanel'
import { DetailTabs } from './components/DetailTabs'
import type { TabDefinition } from './components/DetailTabs'
import { ForecastPanel } from './components/ForecastPanel'
import { DepotPanel, RoutePanel, StationPanel } from './components/NetworkPanel'
import { StaleBanner } from './components/TelemetryBar'
import { StatusBar } from './components/StatusBar'
import { ErrorBox } from './components/common'
import { useBackend } from './lib/useBackend'
import { useTheme } from './lib/useTheme'

export default function App() {
  const data = useBackend()
  const { theme, toggle } = useTheme()

  const risks = useMemo(() => data.plan?.optimization.stockout_risk ?? [], [data.plan])

  const tabs = useMemo<TabDefinition[]>(
    () => [
      {
        id: 'network',
        label: 'Network',
        content: (
          <>
            <DepotPanel depots={data.depots} />
            <div style={{ height: 16 }} />
            <StationPanel stations={data.stations} />
            <div style={{ height: 16 }} />
            <RoutePanel routes={data.routes} />
          </>
        ),
      },
      {
        id: 'forecast',
        label: 'Forecast',
        count: data.forecast?.at_risk_count ?? 0,
        countIsProblem: true,
        content: <ForecastPanel summary={data.forecast} risks={risks} />,
      },
      {
        id: 'problems',
        label: 'Problems',
        count: data.anomalies.length + data.incidents.length,
        countIsProblem: true,
        content: (
          <>
            <AnomalyPanel anomalies={data.anomalies} />
            <div style={{ height: 16 }} />
            <IncidentPanel incidents={data.incidents} />
          </>
        ),
      },
    ],
    [data.depots, data.stations, data.routes, data.forecast, data.anomalies, data.incidents, risks],
  )

  return (
    <div className="app">
      <StatusBar
        health={data.health}
        broker={data.broker}
        metrics={data.metrics}
        stations={data.stations}
        routes={data.routes}
        theme={theme}
        onToggleTheme={toggle}
      />

      <StaleBanner broker={data.broker} />

      {data.error ? <ErrorBox message={`Backend unreachable — ${data.error}`} /> : null}

      <main className="main">
        <ActionCard
          plan={data.plan}
          execution={data.execution}
          busy={data.executeBusy || data.planBusy}
          error={data.actionError}
          onExecute={() => void data.execute()}
          onRecompute={() => void data.recompute()}
        />

        <DetailTabs tabs={tabs} />
      </main>
    </div>
  )
}
