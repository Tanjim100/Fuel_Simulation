/**
 * The console's central promise, made executable.
 *
 * **Every number rendered by the console must be a number the backend sent,
 * or a documented derivation of numbers the backend sent.**
 *
 * Not "close to" one — *exactly* one. The console's credibility rests on the
 * operator being able to trust a screenshot, and a screenshot is only
 * trustworthy if no figure on it was invented in the browser.
 *
 * ## How the allow-list is built
 *
 * The allow-list is **computed from the same derivation functions the panels
 * use**. So:
 *
 * - A panel that derives a value the test does not know about **fails** —
 *   the test catches a new derivation appearing on screen.
 * - A panel that computes a value *differently* from the shared function
 *   **also fails**, because the value would not be in the allow-list.
 *
 * The check therefore covers both directions: no invented figures, and no
 * figure computed by a private shortcut.
 */

import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { AnomalyPanel } from '../components/AnomalyPanel'
import { DecisionPanel } from '../components/DecisionPanel'
import { ForecastPanel } from '../components/ForecastPanel'
import { DepotPanel, RoutePanel, StationPanel } from '../components/NetworkPanel'
import { TelemetryBar } from '../components/TelemetryBar'
import {
  depotTotal,
  fillRatio,
  stationTotal,
} from '../lib/derive'
import {
  anomalies,
  broker,
  depots,
  forecast,
  metrics,
  plan,
  routes,
  stations,
} from './fixtures'

/**
 * Every number-like token in a rendered string.
 *
 * The text is scraped **per leaf element**, not from the concatenated
 * `textContent`. Concatenation glues adjacent elements together — "4,392" in
 * one cell and "0 drops" in the next become "4,3920 drops" — which is a
 * number that was never displayed anywhere. Scraping per element avoids
 * inventing a figure in the test itself.
 */
function scrapeNumbers(root: HTMLElement): number[] {
  const numbers: number[] = []

  const leaves = root.querySelectorAll('*')
  for (const element of leaves) {
    // Only elements with no element children carry their own text.
    if (element.children.length > 0) continue

    const text = element.textContent ?? ''
    const matches = text.match(/-?\d[\d,]*\.?\d*/g)
    if (!matches) continue

    for (const token of matches) {
      const value = Number(token.replace(/,/g, ''))
      if (Number.isFinite(value)) numbers.push(value)
    }
  }

  return numbers
}

/**
 * The numbers the backend sent, plus the renderings of them.
 *
 * The console renders a litre count with thousands separators and rounds a
 * percentage to one decimal, so both renderings of the same value must be
 * allowed.
 */
function backendNumbers(): Set<number> {
  const allowed = new Set<number>()

  const add = (value: number) => {
    if (!Number.isFinite(value)) return
    allowed.add(value)
    allowed.add(Number(value.toFixed(0)))
    allowed.add(Number(value.toFixed(1)))
    allowed.add(Number(value.toFixed(2)))
    allowed.add(Number(value.toFixed(3)))
    // A percentage of a 0..1 fraction, as the console renders it.
    allowed.add(Number((value * 100).toFixed(1)))
    allowed.add(Number((value * 100).toFixed(0)))
    // A ratio, as the console renders it (0 decimals).
    allowed.add(Number((value * 100).toFixed(0)))
  }

  const walk = (value: unknown) => {
    if (typeof value === 'number') {
      add(value)
    } else if (Array.isArray(value)) {
      value.forEach(walk)
    } else if (value && typeof value === 'object') {
      Object.values(value).forEach(walk)
    }
  }

  walk(metrics)
  walk(broker)
  walk(depots)
  walk(stations)
  walk(routes)
  walk(forecast)
  walk(anomalies)
  walk(plan)

  return allowed
}

/**
 * The documented derivations the display layer is allowed to make.
 *
 * Every entry here is a **pure function of backend numbers**, computed by the
 * shared helper in `lib/derive.ts`. If a panel needs a figure that is not in
 * this list, the test fails and the derivation has to be written down here.
 */
function derivedNumbers(): Set<number> {
  const allowed = new Set<number>()

  const add = (value: number) => {
    if (!Number.isFinite(value)) return
    allowed.add(value)
    allowed.add(Number(value.toFixed(0)))
    allowed.add(Number(value.toFixed(1)))
    allowed.add(Number(value.toFixed(2)))
  }

  // Totals: the depot panel sums a depot's stock across its fuels, and the
  // station panel sums a station's stock across its fuels.
  for (const depot of depots) {
    add(depotTotal(depot.inventory))
    add(depotTotal(depot.capacity))
  }
  for (const station of stations) {
    add(stationTotal(station.inventory))
    add(stationTotal(station.capacity))
  }

  // Fill ratios: the station panel renders each tank as a percentage of its
  // capacity.
  for (const station of stations) {
    for (const fuel of ['DIESEL', 'PETROL', 'OCTANE']) {
      const ratio = fillRatio(station.inventory[fuel] ?? 0, station.capacity[fuel] ?? 0)
      add(ratio)
      add(ratio * 100)
    }
  }

  // Dispatch headroom as a fraction of the per-tick cap.
  for (const depot of depots) {
    const ratio = fillRatio(depot.dispatch_remaining, depot.dispatch_capacity_per_tick)
    add(ratio)
    add(ratio * 100)
  }

  // The service level, as the console renders it (a fraction of 100).
  add(metrics.kpis.service_level_pct)
  add(metrics.metrics.service_level)

  // Uptime, rendered as hours and minutes by `duration()`. The components are
  // hours and minutes, so both are legitimate readings.
  const uptimeSeconds = Math.round(broker.broker.uptime_seconds)
  const uptimeMinutes = Math.floor(uptimeSeconds / 60)
  allowed.add(Math.floor(uptimeMinutes / 60))
  allowed.add(uptimeMinutes % 60)

  // The sim clock, rendered as HH:MM by `clockTime()`.
  const simTime = metrics.sim_time
  allowed.add(Number(simTime.slice(11, 13)))
  allowed.add(Number(simTime.slice(14, 16)))

  // Fixed domain constants: the percentage scale, the fuel count, the grid
  // columns, the tick length and the time units.
  ;[0, 1, 2, 3, 15, 24, 60, 96, 100].forEach(add)

  return allowed
}

function assertNoInventedNumbers(container: HTMLElement, label: string) {
  const allowed = new Set([...backendNumbers(), ...derivedNumbers()])
  const numbers = scrapeNumbers(container)

  const invented = numbers.filter((value) => !allowed.has(value))

  expect(
    invented,
    `${label} rendered numbers the backend never sent: ${invented.join(', ')}\n\n` +
      `Rendered text:\n${container.textContent ?? ''}`,
  ).toEqual([])
}

describe('the console invents no numbers', () => {
  it('the telemetry bar renders only backend figures', () => {
    const { container } = render(
      <TelemetryBar
        health={null}
        broker={broker}
        metrics={metrics}
        stations={stations}
        routes={routes}
      />,
    )
    assertNoInventedNumbers(container, 'TelemetryBar')
  })

  it('the depot panel renders only backend figures', () => {
    const { container } = render(<DepotPanel depots={depots} />)
    assertNoInventedNumbers(container, 'DepotPanel')
  })

  it('the station panel renders only backend figures', () => {
    const { container } = render(<StationPanel stations={stations} />)
    assertNoInventedNumbers(container, 'StationPanel')
  })

  it('the route panel renders only backend figures', () => {
    const { container } = render(<RoutePanel routes={routes} />)
    assertNoInventedNumbers(container, 'RoutePanel')
  })

  it('the forecast panel renders only backend figures', () => {
    const { container } = render(<ForecastPanel summary={forecast} risks={[]} />)
    assertNoInventedNumbers(container, 'ForecastPanel')
  })

  it('the anomaly panel renders only backend figures', () => {
    const { container } = render(<AnomalyPanel anomalies={anomalies} />)
    assertNoInventedNumbers(container, 'AnomalyPanel')
  })

  it('the decision panel renders only backend figures', () => {
    const { container } = render(
      <DecisionPanel
        plan={plan}
        execution={null}
        busy={false}
        error={null}
        onExecute={() => {}}
        onRecompute={() => {}}
      />,
    )
    assertNoInventedNumbers(container, 'DecisionPanel')
  })
})
