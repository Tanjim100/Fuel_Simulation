/**
 * The network — depots, stations and routes as the operator sees them.
 *
 * The old rendering was a five-column table per entity, with a nested
 * three-cell meter grid inside a table cell. It rendered every number the
 * backend had, and the operator had to work out which of them mattered.
 *
 * These are **compact rows**: one line per entity, carrying the one figure
 * that changes a decision, with everything else one click away behind a
 * disclosure. The status word becomes a dot — in a list of routes, the word
 * "AVAILABLE" repeated six times is noise, and the colour already says it.
 */

import type { Depot, FuelType, Route, Station } from '../api/types'
import { FUEL_TYPES } from '../api/types'
import { Badge, Dot, Empty, Meter, Panel } from './common'
import { Disclosure } from './Disclosure'
import { count, litres, percent, shortId } from '../lib/format'
import {
  dispatchLevel,
  emptiestTank,
  fillLevel,
  fillRatio,
  statusLevel,
} from '../lib/derive'

export function DepotPanel({ depots }: { depots: Depot[] }) {
  return (
    <Panel title="Depots" flush>
      {depots.length === 0 ? (
        <Empty title="No depots reported" />
      ) : (
        <div className="rows">
          {depots.map((depot) => {
            const level = dispatchLevel(depot.dispatch_remaining, depot.dispatch_capacity_per_tick)
            const ratio = fillRatio(
              depot.dispatch_remaining,
              depot.dispatch_capacity_per_tick,
            )
            return (
              <div className="row" key={depot.id}>
                <Dot level={statusLevel(depot.status)} />
                <div className="row__name">
                  <span className="row__title">{depot.name}</span>
                  <span className="row__meta">{depot.id}</span>
                </div>
                <div className="row__bar">
                  <Meter ratio={ratio} level={level} />
                </div>
                <div className="row__figure">
                  {litres(depot.dispatch_remaining)} L
                  <span className="faint tiny"> of {litres(depot.dispatch_capacity_per_tick)} L</span>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </Panel>
  )
}

/**
 * The stations, one row each.
 *
 * The row shows the *emptiest* tank — the one that is about to run dry — and
 * its fill ratio. Per-fuel detail is one click away, because it is only
 * needed when the operator is deciding *which* fuel to move.
 */
export function StationPanel({
  stations,
  onSelect,
  selectedStationId,
}: {
  stations: Station[]
  onSelect?: (stationId: string) => void
  selectedStationId?: string | null
}) {
  return (
    <Panel title="Stations" flush>
      {stations.length === 0 ? (
        <Empty title="No stations reported" />
      ) : (
        <div className="rows">
          {stations.map((station) => {
            const selected = station.id === selectedStationId
            const tank = emptiestTank(station)
            return (
              <div
                className="row"
                key={station.id}
                onClick={onSelect ? () => onSelect(station.id) : undefined}
                style={selected ? { background: 'var(--accent-bg)' } : undefined}
              >
                <Dot level={statusLevel(station.status)} />
                <div className="row__name">
                  <span className="row__title">{station.name}</span>
                  <span className="row__meta">
                    {station.id}
                    {station.is_single_connected ? ' · single-connected' : ''}
                  </span>
                </div>
                {station.demand_multiplier > 1.05 ? (
                  <Badge level="warn">×{station.demand_multiplier}</Badge>
                ) : null}
                <div className="row__bar">
                  <Meter ratio={tank.ratio} level={tank.level} />
                </div>
                <div className="row__figure">
                  {percent(tank.ratio, 0)}
                  <span className="faint tiny"> {tank.fuel.toLowerCase()}</span>
                </div>
                <StationDetail station={station} />
              </div>
            )
          })}
        </div>
      )}
    </Panel>
  )
}

/** The per-fuel breakdown for one station, behind a disclosure. */
function StationDetail({ station }: { station: Station }) {
  return (
    <Disclosure label="Fuel levels">
      <div className="fuel-grid" style={{ marginTop: 12 }}>
        {FUEL_TYPES.map((fuel) => {
          const inventory = station.inventory?.[fuel] ?? 0
          const capacity = station.capacity?.[fuel] ?? 0
          const ratio = fillRatio(inventory, capacity)
          return (
            <div className="fuel-cell" key={fuel}>
              <div className="fuel-cell__name">{fuel}</div>
              <div className="fuel-cell__value">{percent(ratio, 0)}</div>
              <Meter ratio={ratio} level={fillLevel(ratio)} />
              <div className="fuel-cell__pct">
                {litres(inventory)} / {litres(capacity)} L
              </div>
            </div>
          )
        })}
      </div>
    </Disclosure>
  )
}

/**
 * The routes.
 *
 * One line per route. The route id and the per-shipment maximum are there
 * for the operator who is checking a specific constraint; the everyday
 * reading — is this route open, and how long does it take — is on the line.
 */
export function RoutePanel({ routes }: { routes: Route[] }) {
  const disrupted = routes.filter((r) => r.status !== 'AVAILABLE')
  const single = routes.filter((r) => r.is_single_connected)

  return (
    <Panel
      title="Routes"
      action={
        disrupted.length > 0 ? (
          <Badge level="danger">{count(disrupted.length)} disrupted</Badge>
        ) : (
          <Badge level="ok">all available</Badge>
        )
      }
      flush
    >
      {routes.length === 0 ? (
        <Empty title="No routes reported" />
      ) : (
        <div className="rows">
          {routes.map((route) => (
            <div className="row" key={route.id}>
              <Dot level={statusLevel(route.status)} />
              <div className="row__name">
                <span className="row__title">
                  {shortId(route.source_depot_id)} → {shortId(route.destination_station_id)}
                </span>
                <span className="row__meta">{route.id}</span>
              </div>
              {/*
                * A healthy route needs no word — the dot says it. A broken
                * route does: "disrupted" is the thing the operator acts on.
                */}
              {route.status !== 'AVAILABLE' ? (
                <Badge level={statusLevel(route.status)}>{route.status}</Badge>
              ) : null}
              {route.is_single_connected ? <Badge level="warn">only route</Badge> : null}
              <div className="row__figure">
                {route.transit_ticks} t
                <span className="faint tiny"> · {litres(route.max_shipment)} L max</span>
              </div>
            </div>
          ))}
        </div>
      )}
      {single.length > 0 ? (
        <div style={{ padding: '12px 20px 16px' }}>
          <p className="hint" style={{ margin: 0 }}>
            {count(single.length)} route(s) serve a single-connected station — if one is disrupted,
            that station cannot be rerouted.
          </p>
        </div>
      ) : null}
    </Panel>
  )
}

export type { FuelType }
