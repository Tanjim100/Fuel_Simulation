/**
 * The event stream.
 *
 * The console treats SSE as **advisory**: an event triggers a re-read over
 * REST, and the event payload is never treated as world state. That mirrors
 * the backend's own contract exactly — REST is the truth, SSE is a nudge.
 *
 * These tests pin the behaviour that matters operationally:
 *
 * 1. An event causes a refresh.
 * 2. A dropped stream degrades to polling and says so.
 * 3. A reconnect works.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'

/** A controllable EventSource stand-in. */
class FakeEventSource {
  static instances: FakeEventSource[] = []

  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  closed = false

  private listeners = new Map<string, Array<(event: MessageEvent) => void>>()

  constructor(public url: string) {
    FakeEventSource.instances.push(this)
  }

  addEventListener(name: string, handler: (event: MessageEvent) => void) {
    const existing = this.listeners.get(name) ?? []
    existing.push(handler)
    this.listeners.set(name, existing)
  }

  close() {
    this.closed = true
  }

  // --- test helpers -----------------------------------------------------

  emitOpen() {
    this.onopen?.()
  }

  emitMessage(data = '{}') {
    this.onmessage?.(new MessageEvent('message', { data }))
  }

  emitNamed(name: string, data = '{}') {
    for (const handler of this.listeners.get(name) ?? []) {
      handler(new MessageEvent(name, { data }))
    }
  }

  emitError() {
    this.onerror?.()
  }
}

describe('the event stream is advisory', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('opens a subscription to the backend stream', async () => {
    const refresh = vi.spyOn(api, 'health').mockResolvedValue({
      status: 'ok',
      service: 's',
      version: '1',
      upstream: { reachable: true, status: 'ok', error: null },
      broker: { mode: 'sse', events_received: 0 },
      journal: { path: '/p', total: 0, by_status: {} },
    })
    vi.spyOn(api, 'streamHealth').mockResolvedValue({
      status: 'OK',
      broker: {
        mode: 'sse',
        subscribers: 0,
        events_received: 0,
        events_fanned_out: 0,
        events_dropped: 0,
        reconnects: 0,
        suspected_drops: 0,
        poll_count: 0,
        snapshot_refreshes: 0,
        last_tick_seen: 0,
        seconds_since_last_event: 0,
        sse_connected_since: 0,
        sse_reachable: true,
        last_probe_at: 0,
        probe_failures: 0,
        resync_required: false,
        forced_reconnects: 0,
        uptime_seconds: 0,
        last_error: null,
      },
      snapshot: { tick: 0, age_seconds: 0, is_stale: false, refresh_count: 0, last_error: null },
    })
    for (const [name, value] of [
      ['metrics', {
        status: 'OK', tick: 0, sim_time: '2026-01-01T00:00:00',
        metrics: { served_demand_liters: 0, unmet_demand_liters: 0, service_level: 1, allocation_liters: 0, allocation_failures: 0 },
        kpis: { service_level_pct: 100, unmet_demand_litres: 0, allocation_failures: 0, active_shipments: 0, disrupted_routes: 0, closed_stations: 0 },
      }],
      ['depots', { status: 'OK', tick: 0, depots: [] }],
      ['stations', { status: 'OK', tick: 0, stations: [] }],
      ['routes', { status: 'OK', tick: 0, routes: [] }],
      ['forecastSummary', { status: 'OK', tick: 0, horizon: 16, total_demand_litres: 0, at_risk_count: 0, stations: [] }],
      ['anomalies', { status: 'OK', tick: 0, count: 0, anomalies: [] }],
      ['incidents', { status: 'OK', tick: 0, count: 0, incidents: [] }],
    ] as const) {
      vi.spyOn(api, name as 'metrics').mockResolvedValue(value as never)
    }
    vi.spyOn(api, 'optimize').mockResolvedValue({
      status: 'OK',
      plan_id: 'p',
      tick: 0,
      horizon: 16,
      optimization: {
        status: 'EMPTY',
        solver_used: 'greedy',
        total_litres: 0,
        expected_service_level: 1,
        unmet_demand_litres: 0,
        per_tick_dispatch: {},
        shipments_now: [],
        shipments_later: [],
        shipments: [],
        stockout_risk: [],
        warnings: [],
        diagnostics: {},
      },
      gated: { accepted: 0, rejected: 0, total_litres: 0 },
      explanation: {
        schema_version: '1.0', kind: 'plan', title: '', headline: '', details: [],
        numbers: {}, verdict: 'INFORMATIONAL', alternatives: [], risks: [], next_actions: [], evidence: {},
      },
    })

    const { useBackend } = await import('../lib/useBackend')
    const { renderHook, waitFor } = await import('@testing-library/react')

    renderHook(() => useBackend())

    await waitFor(() => {
      expect(FakeEventSource.instances.length).toBeGreaterThan(0)
    })

    const source = FakeEventSource.instances[0]
    expect(source).toBeDefined()
    expect(source?.url).toBe('/api/events/stream')

    // The stream opens -> the console reports itself live.
    source?.emitOpen()
    await waitFor(() => {
      expect(refresh).toHaveBeenCalled()
    })
  })

  it('re-reads the world when an event arrives', async () => {
    const calls: string[] = []
    vi.spyOn(api, 'health').mockImplementation(async () => {
      calls.push('health')
      return {
        status: 'ok', service: 's', version: '1',
        upstream: { reachable: true, status: 'ok', error: null },
        broker: { mode: 'sse', events_received: 0 },
        journal: { path: '/p', total: 0, by_status: {} },
      }
    })
    vi.spyOn(api, 'streamHealth').mockImplementation(async () => {
      calls.push('streamHealth')
      return {
        status: 'OK',
        broker: {
          mode: 'sse', subscribers: 0, events_received: 0, events_fanned_out: 0, events_dropped: 0,
          reconnects: 0, suspected_drops: 0, poll_count: 0, snapshot_refreshes: 0, last_tick_seen: 0,
          seconds_since_last_event: 0, sse_connected_since: 0, sse_reachable: true, last_probe_at: 0,
          probe_failures: 0, resync_required: false, forced_reconnects: 0, uptime_seconds: 0, last_error: null,
        },
        snapshot: { tick: 0, age_seconds: 0, is_stale: false, refresh_count: 0, last_error: null },
      }
    })
    for (const name of ['metrics', 'depots', 'stations', 'routes', 'forecastSummary', 'anomalies', 'incidents'] as const) {
      vi.spyOn(api, name).mockResolvedValue({} as never)
    }
    vi.spyOn(api, 'optimize').mockResolvedValue({} as never)

    const { useBackend } = await import('../lib/useBackend')
    const { renderHook, waitFor } = await import('@testing-library/react')

    const { result } = renderHook(() => useBackend())

    await waitFor(() => {
      expect(FakeEventSource.instances.length).toBeGreaterThan(0)
    })

    const source = FakeEventSource.instances[0]
    source?.emitOpen()

    await waitFor(() => {
      expect(result.current.streamState).toBe('live')
    })

    const before = calls.filter((c) => c === 'health').length

    // A named simulator event triggers a re-read.
    source?.emitNamed('simulation.tick', JSON.stringify({ tick: 8 }))

    await waitFor(() => {
      expect(calls.filter((c) => c === 'health').length).toBeGreaterThan(before)
    })
  })

  it('degrades to polling when the stream drops, and reconnects', async () => {
    vi.spyOn(api, 'health').mockResolvedValue({
      status: 'ok', service: 's', version: '1',
      upstream: { reachable: true, status: 'ok', error: null },
      broker: { mode: 'sse', events_received: 0 },
      journal: { path: '/p', total: 0, by_status: {} },
    })
    vi.spyOn(api, 'streamHealth').mockResolvedValue({
      status: 'OK',
      broker: {
        mode: 'sse', subscribers: 0, events_received: 0, events_fanned_out: 0, events_dropped: 0,
        reconnects: 0, suspected_drops: 0, poll_count: 0, snapshot_refreshes: 0, last_tick_seen: 0,
        seconds_since_last_event: 0, sse_connected_since: 0, sse_reachable: true, last_probe_at: 0,
        probe_failures: 0, resync_required: false, forced_reconnects: 0, uptime_seconds: 0, last_error: null,
      },
      snapshot: { tick: 0, age_seconds: 0, is_stale: false, refresh_count: 0, last_error: null },
    })
    for (const name of ['metrics', 'depots', 'stations', 'routes', 'forecastSummary', 'anomalies', 'incidents'] as const) {
      vi.spyOn(api, name).mockResolvedValue({} as never)
    }
    vi.spyOn(api, 'optimize').mockResolvedValue({} as never)

    const { useBackend } = await import('../lib/useBackend')
    const { renderHook, waitFor } = await import('@testing-library/react')

    const { result } = renderHook(() => useBackend())

    await waitFor(() => {
      expect(FakeEventSource.instances.length).toBeGreaterThan(0)
    })

    const source = FakeEventSource.instances[0]
    source?.emitOpen()

    await waitFor(() => {
      expect(result.current.streamState).toBe('live')
    })

    // The stream dies.
    source?.emitError()

    await waitFor(() => {
      expect(result.current.streamState).toBe('polling')
    })

    // The old subscription is closed, not left dangling.
    expect(source?.closed).toBe(true)

    // And a fresh one is opened after the backoff. The backoff is a real
    // 3-second timer, so we wait it out rather than faking time — faking
    // time after the timer was already scheduled does not advance it.
    await waitFor(
      () => {
        expect(FakeEventSource.instances.length).toBeGreaterThan(1)
      },
      { timeout: 6000, interval: 100 },
    )
  })
})
