/**
 * The only place the dashboard talks to the backend.
 *
 * Every panel reads its data through here, so there is exactly one place where
 * a URL, a timeout or an error shape is decided. That matters for two reasons:
 * the dashboard must never invent a figure the backend did not send, and the
 * tests must have one seam to stub.
 */

import type {
  Anomaly,
  AutopilotMode,
  AutopilotStatus,
  BrokerHealth,
  Depot,
  ExecuteResponse,
  Forecast,
  ForecastSummary,
  Health,
  Incident,
  Metrics,
  Observability,
  OptimizeResponse,
  Route,
  Station,
} from './types'

/** How long a single request may take before we call it failed. */
const REQUEST_TIMEOUT_MS = 10_000

export class ApiError extends Error {
  readonly status: number
  readonly code: string | undefined

  constructor(message: string, status: number, code?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

type Query = Record<string, string | number | boolean | undefined | null>

function buildUrl(path: string, query?: Query): string {
  const url = new URL(path, window.location.origin)
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === '') continue
      url.searchParams.set(key, String(value))
    }
  }
  return url.pathname + url.search
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)

  try {
    const response = await fetch(path, {
      ...init,
      signal: controller.signal,
      headers: {
        Accept: 'application/json',
        ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
        ...init?.headers,
      },
    })

    const text = await response.text()
    const body = text ? (JSON.parse(text) as unknown) : null

    if (!response.ok) {
      // The backend's error envelope carries the domain code; surface it
      // rather than a generic "request failed".
      const envelope = body as { error?: { code?: string; message?: string } } | null
      throw new ApiError(
        envelope?.error?.message ?? `${response.status} ${response.statusText}`,
        response.status,
        envelope?.error?.code,
      )
    }

    return body as T
  } catch (err) {
    if (err instanceof ApiError) throw err
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiError(`The request to ${path} timed out.`, 0, 'TIMEOUT')
    }
    throw new ApiError(
      err instanceof Error ? err.message : `Could not reach ${path}.`,
      0,
      'NETWORK_ERROR',
    )
  } finally {
    clearTimeout(timer)
  }
}

export const api = {
  health: () => request<Health>('/api/health'),
  streamHealth: () => request<BrokerHealth>('/api/stream-health'),
  metrics: () => request<Metrics>('/api/metrics'),
  /**
   * The observability payload — the JSON mirror of the Prometheus
   * exposition. The console reads this rather than parsing `/metrics`:
   * a text format parsed in the browser would put a number-parsing bug
   * inside the guarantee that every figure on screen came from the
   * backend.
   */
  observability: () => request<Observability>('/api/observability'),
  depots: () => request<{ status: string; tick: number; depots: Depot[] }>('/api/depots'),
  stations: () => request<{ status: string; tick: number; stations: Station[] }>('/api/stations'),
  routes: () => request<{ status: string; tick: number; routes: Route[] }>('/api/routes'),

  forecast: () => request<Forecast>('/api/forecast'),
  forecastSummary: () => request<ForecastSummary>('/api/forecast/summary'),

  anomalies: (query?: Query) =>
    request<{ status: string; tick: number; count: number; anomalies: Anomaly[] }>(
      buildUrl('/api/anomalies', query),
    ),
  incidents: () =>
    request<{ status: string; tick: number; count: number; incidents: Incident[] }>('/api/incidents'),

  optimize: (body: Record<string, unknown> = {}) =>
    request<OptimizeResponse>('/api/optimize', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  execute: (body: Record<string, unknown> = {}) =>
    request<ExecuteResponse>('/api/optimize/execute', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /**
   * The autopilot's state. Read-only: the autopilot's mode is the backend's
   * business, and the console merely shows it.
   */
  autopilot: () => request<AutopilotStatus>('/api/autopilot'),

  /**
   * Switch the autopilot's mode.
   *
   * The backend answers 400 for an unknown mode rather than clamping it, so
   * a typo can never leave the console believing the autopilot is running
   * when it is not.
   */
  setAutopilotMode: (mode: AutopilotMode) =>
    request<AutopilotStatus>('/api/autopilot/mode', {
      method: 'POST',
      body: JSON.stringify({ mode }),
    }),
}
