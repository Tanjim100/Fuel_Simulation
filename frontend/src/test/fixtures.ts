/**
 * Fixtures.
 *
 * These payloads are **copied from real backend responses**, not invented.
 * The shapes match what the simulator and the backend actually return, so a
 * test that passes here is a test against reality.
 */

import type {
  Anomaly,
  BrokerHealth,
  Depot,
  ForecastSummary,
  Health,
  Incident,
  Metrics,
  Observability,
  OptimizeResponse,
  Route,
  Station,
} from '../api/types'

export const health: Health = {
  status: 'ok',
  service: 'buetcsefest-backend',
  version: '1.0.0',
  upstream: { reachable: true, status: 'ok', error: null },
  broker: { mode: 'sse', events_received: 4392 },
  journal: { path: '/app/data/journal.sqlite3', total: 6, by_status: { CREATED: 6 } },
}

export const broker: BrokerHealth = {
  status: 'OK',
  broker: {
    mode: 'sse',
    subscribers: 1,
    events_received: 4392,
    events_fanned_out: 12,
    events_dropped: 0,
    reconnects: 0,
    suspected_drops: 0,
    poll_count: 2437,
    snapshot_refreshes: 6829,
    last_tick_seen: 7,
    seconds_since_last_event: 1.2,
    sse_connected_since: 1795545076.25,
    sse_reachable: true,
    last_probe_at: 1795575201.33,
    probe_failures: 0,
    resync_required: false,
    forced_reconnects: 0,
    uptime_seconds: 30564.2,
    last_error: null,
  },
  snapshot: {
    tick: 7,
    age_seconds: 0.867,
    is_stale: false,
    refresh_count: 6829,
    last_error: null,
  },
}

export const metrics: Metrics = {
  status: 'OK',
  tick: 7,
  sim_time: '2026-01-01T01:45:00',
  metrics: {
    served_demand_liters: 4517.92,
    unmet_demand_liters: 0,
    service_level: 1,
    allocation_liters: 3000,
    allocation_failures: 0,
  },
  kpis: {
    service_level_pct: 100,
    unmet_demand_litres: 0,
    allocation_failures: 0,
    active_shipments: 0,
    disrupted_routes: 1,
    closed_stations: 0,
  },
}

export const depots: Depot[] = [
  {
    id: 'depot-gazipur',
    name: 'Gazipur Depot',
    region_id: 'region-dhaka',
    status: 'OPEN',
    dispatch_capacity_per_tick: 12000,
    capacity: { DIESEL: 90000, PETROL: 70000, OCTANE: 45000 },
    inventory: { DIESEL: 57000, PETROL: 45000, OCTANE: 26000 },
    dispatch_committed_this_tick: 0,
    dispatch_remaining: 12000,
  },
  {
    id: 'depot-patiya',
    name: 'Patiya Depot',
    region_id: 'region-chattogram',
    status: 'OPEN',
    dispatch_capacity_per_tick: 11000,
    capacity: { DIESEL: 85000, PETROL: 65000, OCTANE: 40000 },
    inventory: { DIESEL: 55000, PETROL: 42000, OCTANE: 24000 },
    dispatch_committed_this_tick: 0,
    dispatch_remaining: 11000,
  },
]

export const stations: Station[] = [
  {
    id: 'station-mirpur',
    name: 'Mirpur Fuel Station',
    region_id: 'region-dhaka',
    status: 'OPEN',
    demand_profile: 'urban_high',
    demand_multiplier: 1,
    capacity: { DIESEL: 15000, PETROL: 14000, OCTANE: 9000 },
    inventory: { DIESEL: 11573.776, PETROL: 8465.376, OCTANE: 4720.086 },
    free_space: { DIESEL: 3426.224, PETROL: 5534.624, OCTANE: 4279.914 },
    inbound_in_flight: { DIESEL: 0, PETROL: 0, OCTANE: 0 },
    spill_exposure: { DIESEL: 0, PETROL: 0, OCTANE: 0 },
    route_count: 2,
    is_single_connected: false,
  },
  {
    id: 'station-tongi',
    name: 'Tongi Industrial Station',
    region_id: 'region-dhaka',
    status: 'OPEN',
    demand_profile: 'industrial',
    demand_multiplier: 1.8,
    capacity: { DIESEL: 20000, PETROL: 16000, OCTANE: 8000 },
    inventory: { DIESEL: 1200, PETROL: 900, OCTANE: 6400 },
    free_space: { DIESEL: 18800, PETROL: 15100, OCTANE: 1600 },
    inbound_in_flight: { DIESEL: 0, PETROL: 0, OCTANE: 0 },
    spill_exposure: { DIESEL: 0, PETROL: 0, OCTANE: 0 },
    route_count: 1,
    is_single_connected: true,
  },
]

export const routes: Route[] = [
  {
    id: 'route-gazipur-mirpur',
    source_depot_id: 'depot-gazipur',
    destination_station_id: 'station-mirpur',
    transit_ticks: 2,
    max_shipment: 7000,
    status: 'DISRUPTED',
    destination_route_count: 2,
    is_single_connected: false,
  },
  {
    id: 'route-gazipur-tongi',
    source_depot_id: 'depot-gazipur',
    destination_station_id: 'station-tongi',
    transit_ticks: 2,
    max_shipment: 6500,
    status: 'AVAILABLE',
    destination_route_count: 1,
    is_single_connected: true,
  },
]

export const forecast: ForecastSummary = {
  status: 'OK',
  tick: 7,
  horizon: 16,
  total_demand_litres: 10301.666,
  at_risk_count: 0,
  stations: [
    {
      station_id: 'station-mirpur',
      fuel_type: 'DIESEL',
      current_inventory_litres: 11573.776,
      total_demand_litres: 985.35,
      ticks_to_stockout: null,
      stockout_probability: 0,
      expected_unmet_litres: 0,
      coverage_ticks: 16,
      confidence: 'LOW',
    },
    {
      station_id: 'station-tongi',
      fuel_type: 'DIESEL',
      current_inventory_litres: 1200,
      total_demand_litres: 1055.492,
      ticks_to_stockout: 2,
      stockout_probability: 0.82,
      expected_unmet_litres: 400.2,
      coverage_ticks: 2,
      confidence: 'LOW',
    },
  ],
}

export const anomalies: Anomaly[] = [
  {
    id: '0a4478ac9b57',
    kind: 'ROUTE_BOTTLENECK',
    severity: 'HIGH',
    entity_type: 'ROUTE',
    entity_id: 'route-gazipur-mirpur',
    tick: 7,
    detected_tick: 7,
    title: 'Route route-gazipur-mirpur is DISRUPTED',
    detail:
      'Route route-gazipur-mirpur (depot-gazipur -> station-mirpur) is DISRUPTED. Another route may serve this station.',
    metrics: { transit_ticks: 2, max_shipment: 7000, route_count_for_station: 2 },
    baseline: { max_shipment: 7000 },
    recommended_action: 'Reroute through the alternative depot.',
  },
]

export const incidents: Incident[] = [
  {
    id: '1585afeee198',
    title: 'Route Bottleneck affecting route-gazipur-mirpur',
    severity: 'HIGH',
    entity_type: 'ROUTE',
    entity_id: 'route-gazipur-mirpur',
    start_tick: 7,
    end_tick: 7,
    anomaly_ids: ['0a4478ac9b57'],
    kinds: ['ROUTE_BOTTLENECK'],
    summary:
      '1 anomaly on route-gazipur-mirpur between tick 7 and tick 7: route bottleneck.',
  },
]

export const plan: OptimizeResponse = {
  status: 'OK',
  plan_id: 'plan-t7-4fb4631aa29b',
  tick: 7,
  horizon: 16,
  optimization: {
    status: 'PLANNED',
    solver_used: 'greedy',
    total_litres: 7000,
    expected_service_level: 0.9157,
    unmet_demand_litres: 0,
    per_tick_dispatch: { '7': { 'depot-patiya': 7000 } },
    shipments_now: [
      {
        sequence: 1,
        dispatch_tick: 7,
        arrival_tick: 10,
        source_depot_id: 'depot-patiya',
        destination_station_id: 'station-mirpur',
        route_id: 'route-patiya-mirpur',
        fuel_type: 'DIESEL',
        quantity: 7000,
        priority_score: 0.9,
        rationale: 'Mirpur DIESEL is projected to fall short within the horizon.',
        binding: 'ROUTE_MAX_SHIPMENT',
        risk: 'NONE',
      },
    ],
    shipments_later: [
      {
        sequence: 2,
        dispatch_tick: 9,
        arrival_tick: 12,
        source_depot_id: 'depot-gazipur',
        destination_station_id: 'station-mirpur',
        route_id: 'route-gazipur-mirpur',
        fuel_type: 'PETROL',
        quantity: 3000,
        priority_score: 0.4,
        rationale: 'Deferred to a later tick.',
        binding: 'DISPATCH_CAPACITY',
        risk: 'SPILL_RISK',
      },
    ],
    shipments: [],
    stockout_risk: [],
    warnings: [],
    diagnostics: { horizon: 16 },
  },
  gated: {
    accepted: 2,
    rejected: 0,
    total_litres: 10000,
    executable_now: 1,
    deferred_to_future_ticks: 1,
  },
  explanation: {
    schema_version: '1.0',
    kind: 'plan',
    title: 'Dispatch plan',
    headline: 'Ship 7000 L of DIESEL from Patiya Depot to Mirpur Fuel Station now.',
    details: ['The plan covers the projected shortfall over the next 16 ticks.'],
    numbers: { total_litres: 7000, service_level: 0.9157 },
    verdict: 'RECOMMENDED',
    alternatives: ['Wait one tick and use the gazipur route.'],
    risks: ['Gazipur route is disrupted.'],
    next_actions: ['Dispatch the current-tick shipment.'],
    evidence: {},
  },
}

/**
 * The observability payload — copied from a real `/api/observability`
 * response, so a test that passes here is a test against reality.
 */
export const observability: Observability = {
  status: 'OK',
  components: {
    backend_api: { state: 'HEALTHY' },
    redis: { state: 'HEALTHY' },
    fuel_simulator: { state: 'HEALTHY' },
    prediction_service: { state: 'HEALTHY' },
    decision_engine: { state: 'HEALTHY' },
  },
  latency: { p95_seconds: 0.02425, p50_seconds: 0.0041, avg_seconds: 0.0062 },
  requests: { total: 12681, errors: 0, error_rate: 0 },
  circuit: {
    state: 'CLOSED',
    consecutive_failures: 0,
    opened_at: null,
    cooldown_remaining: 0,
    total_failures: 0,
    total_rejections: 0,
    transitions: 0,
  },
  broker: {
    mode: 'sse',
    events_received: 4392,
    suspected_drops: 0,
    probe_failures: 0,
    reconnects: 0,
  },
  upstream: { reachable: true, error: null },
  snapshot: { tick: 7, age_seconds: 0.867 },
  dlq: { size: 0 },
  metric_families: {
    application: ['backend_http_requests_total'],
    system: ['backend_simulator_pool_saturation_ratio'],
    intelligence: ['backend_fallback_activations_total'],
    resilience: ['backend_circuit_state'],
  },
  metrics: {
    counters: {
      backend_fallback_activations_total: {
        'kind=degraded_mode': 0,
        'kind=poll_fallback': 0,
        'kind=breaker_open': 0,
        'kind=forecast_fallback': 0,
      },
      backend_circuit_rejections_total: { '': 0 },
    },
    gauges: {
      backend_simulator_pool_saturation_ratio: { '': 0.25 },
      backend_simulator_inflight_reads: { '': 1 },
      backend_simulator_fetch_concurrency_limit: { '': 4 },
      backend_simulator_read_queue_depth: { '': 0 },
    },
    histograms: {},
  },
}
