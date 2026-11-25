/**
 * The shapes the backend actually returns.
 *
 * These are hand-written rather than generated, because the backend is the
 * contract: the point of the dashboard is to show exactly what the backend
 * says, so a mismatch here must be a compile error rather than a silently
 * `undefined` on screen.
 */

export type FuelType = 'DIESEL' | 'PETROL' | 'OCTANE'

export const FUEL_TYPES: FuelType[] = ['DIESEL', 'PETROL', 'OCTANE']

export interface Health {
  status: string
  service: string
  version: string
  upstream: { reachable: boolean; status: string; error: string | null }
  broker: { mode: string; events_received: number }
  journal: { path: string; total: number; by_status: Record<string, number> }
}

export interface BrokerHealth {
  status: string
  broker: {
    mode: 'sse' | 'polling' | 'starting' | 'stopped'
    subscribers: number
    events_received: number
    events_fanned_out: number
    events_dropped: number
    reconnects: number
    suspected_drops: number
    poll_count: number
    snapshot_refreshes: number
    last_tick_seen: number
    seconds_since_last_event: number
    sse_connected_since: number
    sse_reachable: boolean
    last_probe_at: number
    probe_failures: number
    resync_required: boolean
    forced_reconnects: number
    uptime_seconds: number
    last_error: string | null
  }
  snapshot: {
    tick: number
    age_seconds: number
    is_stale: boolean
    refresh_count: number
    last_error: string | null
  }
}

export interface Depot {
  id: string
  name: string
  region_id: string
  status: string
  dispatch_capacity_per_tick: number
  capacity: Record<string, number>
  inventory: Record<string, number>
  dispatch_committed_this_tick: number
  dispatch_remaining: number
}

export interface Station {
  id: string
  name: string
  region_id: string
  status: string
  demand_profile: string
  demand_multiplier: number
  capacity: Record<string, number>
  inventory: Record<string, number>
  free_space: Record<string, number>
  inbound_in_flight: Record<string, number>
  spill_exposure: Record<string, number>
  route_count: number
  is_single_connected: boolean
}

export interface Route {
  id: string
  source_depot_id: string
  destination_station_id: string
  transit_ticks: number
  max_shipment: number
  status: string
  destination_route_count: number
  is_single_connected: boolean
}

export interface Metrics {
  status: string
  tick: number
  sim_time: string
  metrics: {
    served_demand_liters: number
    unmet_demand_liters: number
    service_level: number
    allocation_liters: number
    allocation_failures: number
  }
  kpis: {
    service_level_pct: number
    unmet_demand_litres: number
    allocation_failures: number
    active_shipments: number
    disrupted_routes: number
    closed_stations: number
  }
}

export interface StockoutRisk {
  station_id: string
  fuel_type: string
  current_inventory: number
  projected_inventory: number[]
  demand_series: number[]
  ticks_to_stockout: number | null
  stockout_tick: number | null
  stockout_probability: number
  expected_unmet_litres: number
  coverage_ticks: number
  confidence: string
}

export interface PlannedShipment {
  sequence: number
  dispatch_tick: number
  arrival_tick: number
  source_depot_id: string
  destination_station_id: string
  route_id: string
  fuel_type: string
  quantity: number
  priority_score: number
  rationale: string
  binding: string
  risk: string
}

export interface Optimization {
  status: string
  solver_used: string
  total_litres: number
  expected_service_level: number
  unmet_demand_litres: number
  per_tick_dispatch: Record<string, Record<string, number>>
  shipments_now: PlannedShipment[]
  shipments_later: PlannedShipment[]
  shipments: PlannedShipment[]
  stockout_risk: StockoutRisk[]
  warnings: string[]
  diagnostics: Record<string, unknown>
}

export interface GatedSummary {
  accepted: number
  rejected: number
  total_litres: number
  rejections?: unknown[]
  executable_now?: number
  deferred_to_future_ticks?: number
}

export interface Explanation {
  schema_version: string
  kind: string
  title: string
  headline: string
  details: string[]
  numbers: Record<string, number>
  verdict: string
  alternatives: string[]
  risks: string[]
  next_actions: string[]
  evidence: Record<string, unknown>
}

export interface OptimizeResponse {
  status: string
  plan_id: string
  tick: number
  horizon: number
  optimization: Optimization
  gated: GatedSummary
  explanation: Explanation
}

export interface ExecuteResponse extends OptimizeResponse {
  created: Array<Record<string, unknown>>
  replayed: Array<Record<string, unknown>>
  failed: Array<Record<string, unknown>>
  total_litres: number
}

export interface Anomaly {
  id: string
  kind: string
  severity: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW'
  entity_type: string
  entity_id: string
  tick: number
  detected_tick: number
  title: string
  detail: string
  metrics: Record<string, number>
  baseline: Record<string, number>
  recommended_action: string
}

export interface Incident {
  id: string
  title: string
  severity: string
  entity_type: string
  entity_id: string
  start_tick: number
  end_tick: number
  anomaly_ids: string[]
  kinds: string[]
  summary: string
  explanation?: Explanation
}

export interface ForecastSummaryEntry {
  station_id: string
  fuel_type: string
  current_inventory_litres: number
  total_demand_litres: number
  ticks_to_stockout: number | null
  stockout_probability: number
  expected_unmet_litres: number
  coverage_ticks: number
  confidence: string
}

export interface ForecastSummary {
  status: string
  tick: number
  horizon: number
  total_demand_litres: number
  at_risk_count: number
  stations: ForecastSummaryEntry[]
}

export interface Forecast {
  status: string
  tick: number
  horizon: number
  seed: number
  start_tick: number
  demand: Record<string, number[]>
  stockout_risk?: StockoutRisk[]
}
