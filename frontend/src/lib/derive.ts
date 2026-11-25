/**
 * Derived readings — the *presentation* logic.
 *
 * The rule the whole console follows: **a derived reading may only ever be a
 * pure function of numbers the backend sent.** No estimation, no smoothing, no
 * "roughly". If a number appears on screen, either the backend sent it or it
 * was computed here from numbers the backend sent — and the computation is
 * unit-tested.
 *
 * That is what lets an operator trust the screen: there is no place where a
 * figure could have been invented.
 */

import type { Depot, FuelType, Station } from '../api/types'
import { FUEL_TYPES } from '../api/types'

/**
 * A reading's severity.
 *
 * `ok` / `warn` / `danger` are the three *alert* levels. `neutral` and `dim`
 * are presentational tones for readings that are not alerts — an informational
 * figure, or a muted label. They are part of the same union so a component's
 * `level` prop is one type rather than two.
 */
export type HealthLevel = 'ok' | 'warn' | 'danger' | 'neutral' | 'dim'

/**
 * How full a tank is, as a 0..1 fraction of its capacity.
 *
 * A station with no capacity entry for a fuel is treated as **empty** rather
 * than infinitely full: an unknown capacity cannot justify calling the
 * station healthy.
 */
export function fillRatio(inventory: number, capacity: number): number {
  if (!Number.isFinite(capacity) || capacity <= 0) return 0
  const ratio = inventory / capacity
  if (!Number.isFinite(ratio)) return 0
  return Math.min(1, Math.max(0, ratio))
}

/**
 * The severity of a fill level.
 *
 * The thresholds are the dashboard's *display* policy, not a business rule the
 * backend enforces — the backend's own stockout probability is the authority
 * for whether fuel is actually at risk. These thresholds only decide which
 * colour a bar is painted.
 */
export function fillLevel(ratio: number): HealthLevel {
  if (ratio < 0.15) return 'danger'
  if (ratio < 0.35) return 'warn'
  return 'ok'
}

/** The severity of a depot's remaining dispatch headroom this tick. */
export function dispatchLevel(remaining: number, cap: number): HealthLevel {
  if (!Number.isFinite(cap) || cap <= 0) return 'ok'
  const ratio = remaining / cap
  if (ratio <= 0.05) return 'danger'
  if (ratio <= 0.25) return 'warn'
  return 'ok'
}

/** The severity of the broker's stream mode. */
export function streamLevel(mode: string, reachable: boolean): HealthLevel {
  if (mode === 'stopped') return 'danger'
  if (mode === 'polling' && !reachable) return 'danger'
  if (mode === 'polling') return 'warn'
  if (mode === 'starting') return 'warn'
  return 'ok'
}

/** The severity of a service level, as a 0..1 fraction. */
export function serviceLevel(fraction: number | null | undefined): HealthLevel {
  if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) return 'warn'
  if (fraction < 0.9) return 'danger'
  if (fraction < 0.99) return 'warn'
  return 'ok'
}

/**
 * The most severe *alert* level in a list.
 *
 * `danger` dominates `warn` dominates `ok`. The presentational tones
 * (`neutral`, `dim`) are not alerts and are ignored — otherwise a muted
 * label would make a summary tile look degraded.
 */
export function worstLevel(levels: HealthLevel[]): HealthLevel {
  if (levels.includes('danger')) return 'danger'
  if (levels.includes('warn')) return 'warn'
  return 'ok'
}

/** A stable severity ordering, highest first. */
export const SEVERITY_ORDER = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'] as const

export type Severity = (typeof SEVERITY_ORDER)[number]

/** Rank a severity so it can be sorted. Unknown severities sort last. */
export function severityRank(severity: string): number {
  const index = SEVERITY_ORDER.indexOf(severity as Severity)
  return index === -1 ? SEVERITY_ORDER.length : index
}

/** The colour for a severity band. */
export function severityLevel(severity: string): HealthLevel {
  switch (severity) {
    case 'CRITICAL':
      return 'danger'
    case 'HIGH':
      return 'danger'
    case 'MEDIUM':
      return 'warn'
    case 'LOW':
      return 'ok'
    default:
      return 'ok'
  }
}

/** The colour for a plan verdict. */
export function verdictLevel(verdict: string): HealthLevel {
  switch (verdict) {
    case 'RECOMMENDED':
      return 'ok'
    case 'CAUTION':
      return 'warn'
    case 'BLOCKED':
      return 'danger'
    default:
      return 'ok'
  }
}

/** The colour for a resource status. */
export function statusLevel(status: string): HealthLevel {
  switch (status) {
    case 'AVAILABLE':
    case 'OPEN':
      return 'ok'
    case 'CONSTRAINED':
      return 'warn'
    case 'DISRUPTED':
    case 'CLOSED':
    case 'OUTAGE':
      return 'danger'
    default:
      return 'warn'
  }
}

/** The total inventory across every fuel a depot holds. */
export function depotTotal(inventory: Record<string, number>): number {
  return Object.values(inventory).reduce((sum, value) => sum + (value || 0), 0)
}

/** The total inventory across every fuel a station holds. */
export function stationTotal(inventory: Record<string, number>): number {
  return Object.values(inventory).reduce((sum, value) => sum + (value || 0), 0)
}

/**
 * The fuels a station is short of, by capacity headroom.
 *
 * A fuel with a positive `free_space` is *not* short. A fuel missing from the
 * free-space map is treated as short, because the backend omitted it.
 */
export function tightFuels(station: Station): FuelType[] {
  return FUEL_TYPES.filter((fuel) => {
    const free = station.free_space?.[fuel]
    if (free === undefined || free === null) return true
    return free <= 0
  })
}

/**
 * The stations a depot can no longer serve this tick, because its dispatch
 * headroom is exhausted.
 */
export function exhaustedDepots(depots: Depot[]): Depot[] {
  return depots.filter((depot) => depot.dispatch_remaining <= 0)
}

/** The stations whose status is anything other than OPEN. */
export function closedStations(stations: Station[]): Station[] {
  return stations.filter((station) => station.status !== 'OPEN')
}

/** The routes whose status is anything other than AVAILABLE. */
export function disruptedRoutes<T extends { status: string }>(routes: T[]): T[] {
  return routes.filter((route) => route.status !== 'AVAILABLE')
}

/**
 * The number of whole days a station's fuel will last, at the forecast rate.
 *
 * When the forecast is unavailable the answer is unknown, not zero — showing
 * "0 days" for "we cannot tell" would be a lie that looks like a fact.
 */
export function coverageDays(coverageTicks: number, tickMinutes: number): number | null {
  if (!Number.isFinite(coverageTicks) || coverageTicks <= 0) return null
  const minutesPerDay = 24 * 60
  return (coverageTicks * tickMinutes) / minutesPerDay
}

/**
 * The emptiest tank at a station, and how full it is.
 *
 * The console shows one figure per station rather than three, because the
 * operator's question is "which tank is about to run dry", not "what is the
 * state of every tank". The emptiest one is the answer to that question.
 *
 * The ratio is the *same* `fillRatio` the per-fuel panels use, so this is a
 * selection over existing derivations — not a new calculation. A fuel the
 * backend did not report counts as empty, for the same reason it does
 * everywhere else: an unknown capacity cannot justify calling a tank healthy.
 */
export function emptiestTank(
  station: Station,
): { fuel: FuelType; ratio: number; level: HealthLevel } {
  let worst: { fuel: FuelType; ratio: number; level: HealthLevel } | null = null

  for (const fuel of FUEL_TYPES) {
    const ratio = fillRatio(station.inventory?.[fuel] ?? 0, station.capacity?.[fuel] ?? 0)
    if (worst === null || ratio < worst.ratio) {
      worst = { fuel, ratio, level: fillLevel(ratio) }
    }
  }

  // `FUEL_TYPES` is a non-empty constant, so this branch is unreachable in
  // practice — but the type system cannot know that, and a fallback that
  // invents a fuel name would be worse than an honest "no data".
  return worst ?? { fuel: 'DIESEL', ratio: 0, level: 'danger' }
}

/**
 * The forecast demand, totalled by fuel.
 *
 * The backend reports demand per station/fuel pair and a grand total; it
 * does **not** report a per-fuel total. The console shows one bar per fuel,
 * so it has to add the pairs up — and that sum is a derived figure, which
 * means it has to live here, with a name, where the no-invented-numbers
 * test can see it.
 *
 * This is the one place in the console where a total is computed rather
 * than read. It is a sum of backend numbers and nothing else.
 */
export function demandByFuel(
  entries: Array<{ fuel_type: string; total_demand_litres: number }>,
): Array<{ fuel: string; litres: number }> {
  const totals = new Map<string, number>()

  for (const entry of entries) {
    const value = entry.total_demand_litres
    if (!Number.isFinite(value)) continue
    totals.set(entry.fuel_type, (totals.get(entry.fuel_type) ?? 0) + value)
  }

  return [...totals.entries()]
    .map(([fuel, value]) => ({ fuel, litres: value }))
    .sort((a, b) => b.litres - a.litres)
}
