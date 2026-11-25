/**
 * The network column — depots, stations and routes as the operator sees them.
 *
 * Every panel is a table over a backend payload. No client-side aggregation
 * of business figures beyond ratios and severity colouring.
 */

import type { Depot, FuelType, Route, Station } from '../api/types'
import { FUEL_TYPES } from '../api/types'
import { Badge, Empty, Meter, Panel } from './common'
import { count, litres, percent, shortId } from '../lib/format'
import {
  dispatchLevel,
  fillLevel,
  fillRatio,
  statusLevel,
} from '../lib/derive'

function fuelTone(station: Station, fuel: FuelType) {
  const inventory = station.inventory?.[fuel] ?? 0
  const capacity = station.capacity?.[fuel] ?? 0
  const ratio = fillRatio(inventory, capacity)
  return { ratio, level: fillLevel(ratio), inventory, capacity }
}

export function DepotPanel({ depots }: { depots: Depot[] }) {
  return (
    <Panel title="Depots">
      {depots.length === 0 ? (
        <Empty title="No depots reported" />
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Depot</th>
              <th>Status</th>
              <th className="num">Dispatch left</th>
              <th className="num">Stock</th>
            </tr>
          </thead>
          <tbody>
            {depots.map((depot) => {
              const level = dispatchLevel(depot.dispatch_remaining, depot.dispatch_capacity_per_tick)
              const ratio = fillRatio(
                depot.dispatch_remaining,
                depot.dispatch_capacity_per_tick,
              )
              return (
                <tr key={depot.id}>
                  <td>
                    <div className="entity">
                      <span className="entity__name">{depot.name}</span>
                      <span className="entity__meta">{depot.id}</span>
                    </div>
                  </td>
                  <td>
                    <Badge level={statusLevel(depot.status)}>{depot.status}</Badge>
                  </td>
                  <td className="num">
                    <div className="meter-row">
                      <Meter ratio={ratio} level={level} />
                      <span>{litres(depot.dispatch_remaining)} L</span>
                    </div>
                    <div className="tiny faint">
                      cap {litres(depot.dispatch_capacity_per_tick)} L/tick
                    </div>
                  </td>
                  <td className="num">{litres(depotTotal(depot.inventory))} L</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </Panel>
  )
}

function depotTotal(inventory: Record<string, number>): number {
  return Object.values(inventory).reduce((sum, value) => sum + (value || 0), 0)
}

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
    <Panel title="Stations">
      {stations.length === 0 ? (
        <Empty title="No stations reported" />
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Station</th>
              <th>Status</th>
              <th>Fuel level</th>
              <th className="num">Free space</th>
            </tr>
          </thead>
          <tbody>
            {stations.map((station) => {
              const selected = station.id === selectedStationId
              return (
                <tr
                  key={station.id}
                  onClick={onSelect ? () => onSelect(station.id) : undefined}
                  style={selected ? { background: 'var(--accent-bg)' } : undefined}
                >
                  <td>
                    <div className="entity">
                      <span className="entity__name">{station.name}</span>
                      <span className="entity__meta">
                        {station.id} · {station.demand_profile}
                        {station.is_single_connected ? ' · single-connected' : ''}
                      </span>
                    </div>
                  </td>
                  <td>
                    <Badge level={statusLevel(station.status)}>{station.status}</Badge>
                    {station.demand_multiplier > 1.05 ? (
                      <>
                        {' '}
                        <Badge level="warn">×{station.demand_multiplier}</Badge>
                      </>
                    ) : null}
                  </td>
                  <td>
                    <div className="fuel-grid">
                      {FUEL_TYPES.map((fuel) => {
                        const { ratio, level } = fuelTone(station, fuel)
                        return (
                          <div className="fuel-cell" key={fuel}>
                            <div className="fuel-cell__name">{fuel.slice(0, 3)}</div>
                            <div className="fuel-cell__value">{percent(ratio, 0)}</div>
                            <Meter ratio={ratio} level={level} />
                          </div>
                        )
                      })}
                    </div>
                  </td>
                  <td className="num">
                    {FUEL_TYPES.map((fuel) => (
                      <div key={fuel} className="tiny">
                        {fuel.slice(0, 3)} {litres(station.free_space?.[fuel])} L
                      </div>
                    ))}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </Panel>
  )
}

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
    >
      {routes.length === 0 ? (
        <Empty title="No routes reported" />
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Route</th>
              <th>Status</th>
              <th className="num">Transit</th>
              <th className="num">Max</th>
            </tr>
          </thead>
          <tbody>
            {routes.map((route) => (
              <tr key={route.id}>
                <td>
                  <div className="entity">
                    <span className="entity__name">
                      {shortId(route.source_depot_id)} → {shortId(route.destination_station_id)}
                    </span>
                    <span className="entity__meta">
                      {route.id}
                      {route.is_single_connected ? ' · only route' : ''}
                    </span>
                  </div>
                </td>
                <td>
                  <Badge level={statusLevel(route.status)}>{route.status}</Badge>
                </td>
                <td className="num">{route.transit_ticks} t</td>
                <td className="num">{litres(route.max_shipment)} L</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {single.length > 0 ? (
        <p className="hint" style={{ marginTop: 12, marginBottom: 0 }}>
          {count(single.length)} route(s) serve a single-connected station — if one is disrupted,
          that station cannot be rerouted.
        </p>
      ) : null}
    </Panel>
  )
}
