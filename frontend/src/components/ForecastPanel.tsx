/**
 * The forecast — projected stockout timelines and demand by fuel.
 *
 * The forecast itself is the backend's (and it is *exact*, not estimated —
 * the simulator's demand is seed-derived). This panel renders the backend's
 * projection and its own stockout probability.
 *
 * The two panels the old console had — "Projected stockout" and "Forecast
 * demand by fuel" — are one now. They were two tables about the same
 * forecast, and an operator had to reconcile them by eye.
 *
 * The one derived reading here is the sparkline, which is drawn from the
 * `projected_inventory` series the backend computed. The console does not
 * project anything itself.
 */

import type { ForecastSummary, StockoutRisk } from '../api/types'
import { Badge, Empty, Meter, Panel } from './common'
import { count, litres, percent, ticks } from '../lib/format'
import { demandByFuel, worstLevel } from '../lib/derive'

/**
 * A tiny inline sparkline.
 *
 * Rendered as an SVG path from the series the backend computed. It is a
 * *display* of the backend's numbers, not a new calculation.
 */
function Sparkline({
  values,
  capacity,
  width = 108,
  height = 26,
}: {
  values: number[]
  capacity: number
  width?: number
  height?: number
}) {
  if (values.length === 0) return null

  const max = Math.max(capacity, ...values, 1)
  const min = Math.min(0, ...values)
  const span = max - min || 1

  const points = values.map((value, index) => {
    const x = (index / Math.max(1, values.length - 1)) * (width - 2) + 1
    const y = height - 1 - ((value - min) / span) * (height - 2)
    return [x, y] as const
  })

  const path = points.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ')
  const last = points[points.length - 1]

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`Projected inventory over ${values.length} ticks`}
      style={{ display: 'block' }}
    >
      {/* The tank's ceiling — the line the inventory must not cross. */}
      <line
        x1="1"
        y1={height - 1 - ((capacity - min) / span) * (height - 2)}
        x2={width - 1}
        y2={height - 1 - ((capacity - min) / span) * (height - 2)}
        stroke="var(--border-strong)"
        strokeWidth="1"
        strokeDasharray="3 3"
      />
      <path d={path} fill="none" stroke="var(--accent)" strokeWidth="1.5" />
      <circle cx={last?.[0]} cy={last?.[1]} r="2.5" fill="var(--accent)" />
    </svg>
  )
}

function riskLevel(risk: StockoutRisk) {
  return worstLevel([
    risk.stockout_probability >= 0.5
      ? 'danger'
      : risk.stockout_probability >= 0.1
        ? 'warn'
        : 'ok',
    risk.ticks_to_stockout !== null && risk.ticks_to_stockout <= 4 ? 'danger' : 'ok',
  ])
}

/**
 * The forecast tab.
 *
 * The demand-by-fuel breakdown sits above the stockout table: what is
 * coming, then which tank is at risk from it. The old layout had them the
 * other way round, in different panels.
 */
export function ForecastPanel({
  summary,
  risks,
}: {
  summary: ForecastSummary | null
  risks: StockoutRisk[]
}) {
  const entries = summary?.stations ?? []

  // The per-fuel totals are a derivation, not a backend field — see
  // `demandByFuel` in lib/derive.ts.
  const fuelEntries = demandByFuel(entries)
  const maxFuel = Math.max(...fuelEntries.map((entry) => entry.litres), 1)

  return (
    <Panel
      title="Forecast"
      action={
        summary && summary.at_risk_count > 0 ? (
          <Badge level="warn">{summary.at_risk_count} at risk</Badge>
        ) : (
          <Badge level="ok">none at risk</Badge>
        )
      }
      flush
    >
      {!summary ? (
        <Empty title="No forecast yet" />
      ) : (
        <>
          <div style={{ padding: '20px 20px 4px' }}>
            <p className="hint" style={{ marginTop: 0 }}>
              Over the next {summary.horizon} ticks, forecast demand totals{' '}
              <span className="mono">{litres(summary.total_demand_litres)} L</span>. Computed
              offline from the simulator's seed — exact, not estimated.
            </p>

            {fuelEntries.length > 0 ? (
              <div style={{ marginTop: 16 }}>
                {fuelEntries.map((entry) => (
                  <div key={entry.fuel} style={{ marginBottom: 10 }}>
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        fontSize: 13,
                        marginBottom: 4,
                      }}
                    >
                      <span className="dim">{entry.fuel}</span>
                      <span className="mono">{litres(entry.litres)} L</span>
                    </div>
                    <Meter ratio={entry.litres / maxFuel} level="ok" />
                  </div>
                ))}
              </div>
            ) : null}
          </div>

          {entries.length === 0 ? (
            <Empty title="No station/fuel pairs in the forecast" />
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Station / fuel</th>
                  <th className="num">Stock</th>
                  <th>Projection</th>
                  <th className="num">Cover</th>
                  <th className="num">Risk</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => {
                  const risk = risks.find(
                    (r) => r.station_id === entry.station_id && r.fuel_type === entry.fuel_type,
                  )
                  const level = risk ? riskLevel(risk) : 'ok'
                  return (
                    <tr key={`${entry.station_id}:${entry.fuel_type}`}>
                      <td>
                        <div className="entity">
                          <span className="entity__name">{entry.fuel_type}</span>
                          <span className="entity__meta">{entry.station_id}</span>
                        </div>
                      </td>
                      <td className="num">{litres(entry.current_inventory_litres)} L</td>
                      <td>
                        {risk ? (
                          <Sparkline
                            values={risk.projected_inventory}
                            capacity={risk.current_inventory + (risk.projected_inventory[0] ?? 0)}
                          />
                        ) : (
                          <span className="faint tiny">—</span>
                        )}
                      </td>
                      <td className="num">
                        {entry.ticks_to_stockout !== null ? (
                          <Badge level="danger">{ticks(entry.ticks_to_stockout)}</Badge>
                        ) : (
                          <span className="dim">{ticks(entry.coverage_ticks)}</span>
                        )}
                      </td>
                      <td className="num">
                        <Badge level={level}>{percent(entry.stockout_probability, 0)}</Badge>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </>
      )}
    </Panel>
  )
}

export { count }
