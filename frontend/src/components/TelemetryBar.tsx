/**
 * The telemetry bar — the operator's first read.
 *
 * Nine numbers that answer "is anything wrong right now?" before the
 * operator scrolls. Every one of them is a figure the backend sent, or a
 * pure function of figures the backend sent.
 */

import type { BrokerHealth, Health, Metrics, Station } from '../api/types'
import { Badge, Stat } from './common'
import { count, duration, litres, percent, percentOfHundred } from '../lib/format'
import {
  closedStations,
  disruptedRoutes,
  serviceLevel as serviceLevelOf,
  streamLevel,
  worstLevel,
} from '../lib/derive'

export function TelemetryBar({
  health,
  broker,
  metrics,
  stations,
  routes,
}: {
  health: Health | null
  broker: BrokerHealth | null
  metrics: Metrics | null
  stations: Station[]
  routes: Array<{ status: string }>
}) {
  const serviceLevel = metrics ? metrics.kpis.service_level_pct / 100 : null
  const serviceState = serviceLevelOf(serviceLevel)

  const disrupted = disruptedRoutes(routes).length
  const closed = closedStations(stations).length
  const problems = worstLevel([serviceState, disrupted > 0 ? 'danger' : 'ok', closed > 0 ? 'danger' : 'ok'])

  const streamState = broker ? streamLevel(broker.broker.mode, broker.broker.sse_reachable) : 'warn'

  return (
    <div className="telemetry">
      <div className="telemetry__brand">
        <h1 className="telemetry__title">Fuel Supply Intelligence</h1>
        <p className="telemetry__subtitle">
          Operator console · {metrics ? `tick ${metrics.tick}` : 'connecting'}
        </p>
      </div>

      <div className="telemetry__stats">
        <Stat
          label="Service level"
          value={metrics ? percentOfHundred(metrics.kpis.service_level_pct) : '—'}
          sub={metrics ? `${litres(metrics.metrics.unmet_demand_liters)} L unmet` : undefined}
          level={serviceState}
        />
        <Stat
          label="Sim time"
          value={metrics ? metrics.sim_time.slice(11, 16) : '—'}
          sub={metrics ? `tick ${metrics.tick}` : undefined}
          level="dim"
        />
        <Stat
          label="Dispatched"
          value={metrics ? `${litres(metrics.metrics.allocation_liters)} L` : '—'}
          sub={metrics ? `${count(metrics.kpis.active_shipments)} active` : undefined}
          level="dim"
        />
        <Stat
          label="Disrupted routes"
          value={count(disrupted)}
          sub={disrupted > 0 ? 'reroute advised' : 'all clear'}
          level={disrupted > 0 ? 'danger' : 'ok'}
        />
        <Stat
          label="Closed stations"
          value={count(closed)}
          sub={closed > 0 ? 'demand stranded' : 'all open'}
          level={closed > 0 ? 'danger' : 'ok'}
        />
        <Stat
          label="Stream"
          value={broker ? broker.broker.mode : '—'}
          sub={broker ? `${duration(broker.broker.uptime_seconds)} uptime` : undefined}
          level={streamState}
        />
        <Stat
          label="Upstream"
          value={health ? (health.upstream.reachable ? 'reachable' : 'down') : '—'}
          sub={health ? health.upstream.status : undefined}
          level={health?.upstream.reachable ? 'ok' : 'danger'}
        />
        <Stat
          label="Dispatches"
          value={health ? count(health.journal.total) : '—'}
          sub={health ? 'journalled' : undefined}
          level="dim"
        />
        <Stat
          label="Events"
          value={broker ? count(broker.broker.events_received) : '—'}
          sub={broker ? `${count(broker.broker.suspected_drops)} drops` : undefined}
          level="dim"
        />
        <div className="stat">
          <span className="stat__label">Overall</span>
          <Badge level={problems} pulse={problems !== 'ok'}>
            {problems === 'ok' ? 'nominal' : problems === 'warn' ? 'degraded' : 'attention'}
          </Badge>
        </div>
      </div>
    </div>
  )
}

/**
 * The stale-data banner.
 *
 * The backend reports whether its snapshot is fresh. A console that shows
 * numbers without saying how old they are is a console that can lie. So the
 * banner is not decoration — it is the freshness contract, made visible.
 */
export function StaleBanner({ broker }: { broker: BrokerHealth | null }) {
  if (!broker) return null

  const { snapshot, broker: b } = broker
  const isStale = snapshot.is_stale
  const unreachable = !b.sse_reachable

  if (!isStale && !unreachable && !b.resync_required) return null

  const level = isStale || unreachable ? 'danger' : 'warn'

  let title = 'The displayed data may be out of date'
  let body = ''

  if (isStale) {
    title = 'Stale data'
    body = `The world snapshot is ${duration(snapshot.age_seconds)} old and the upstream has not confirmed it. Figures below describe the last known state, not the current one.`
  } else if (unreachable) {
    title = 'Upstream unreachable'
    body = `The backend cannot reach the simulator over the event stream. It is polling instead. Figures below are as of ${duration(snapshot.age_seconds)} ago.`
  } else {
    title = 'Resync required'
    body = 'The backend missed events and is re-fetching state. Figures below are as of the last confirmed snapshot.'
  }

  return (
    <div className={`banner banner--${level}`} role="status">
      <div className="banner__text">
        <p className="banner__title">{title}</p>
        <p className="banner__body">{body}</p>
      </div>
    </div>
  )
}

/**
 * The operator's service-level reading, in words.
 *
 * The percentage is the backend's; this only turns it into a sentence.
 */
export function serviceSentence(metrics: Metrics | null): string {
  if (!metrics) return 'Waiting for the first metrics reading.'
  const pct = metrics.kpis.service_level_pct
  if (pct >= 100) return 'Every litre of demand has been served.'
  if (pct >= 95) return 'Demand is being served, with a small shortfall.'
  if (pct >= 80) return 'A material share of demand is going unmet.'
  return 'Most demand is going unmet.'
}

export { percent }
