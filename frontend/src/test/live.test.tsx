/**
 * The console against the **live** backend.
 *
 * This test renders the whole `App` with `fetch` pointed at the running
 * backend, so it exercises the real payload shapes, the real panel wiring and
 * the real "no invented numbers" guarantee — end to end, against the live
 * simulator's world.
 *
 * It is skipped unless `LIVE_BACKEND` is set, because the normal test run
 * must not depend on a running stack:
 *
 * ```bash
 * LIVE_BACKEND=http://localhost:8002 npm test
 * ```
 */

import { describe, expect, it, beforeAll, afterAll, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import App from '../App'

const LIVE_BACKEND = process.env.LIVE_BACKEND

// The live test talks to a running stack over the network. The default 5s
// budget is not enough for a full render plus nine parallel reads, so this
// file opts into a longer one.
vi.setConfig({ testTimeout: 60_000, hookTimeout: 60_000 })

let originalFetch: typeof globalThis.fetch

beforeAll(() => {
  if (!LIVE_BACKEND) return

  originalFetch = globalThis.fetch

  // The browser talks to a single origin (the console's own server). In the
  // test we are the browser, so we point fetch at the backend directly.
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url

    if (url.startsWith('/api/')) {
      return originalFetch(`${LIVE_BACKEND}${url}`, init)
    }
    return originalFetch(input as RequestInfo, init)
  }) as typeof globalThis.fetch
})

afterAll(() => {
  if (originalFetch) globalThis.fetch = originalFetch
})

describe('the console against the live backend', () => {
  it.skipIf(!LIVE_BACKEND)('renders the live world', async () => {
    render(<App />)

    // The telemetry bar populates from the real metrics endpoint. Wait for
    // the *tick number*, not the label — the label renders before the data
    // arrives.
    await waitFor(
      () => {
        const bar = document.querySelector('.telemetry')
        expect(bar?.textContent ?? '').toMatch(/tick\s+\d+/i)
      },
      { timeout: 30000 },
    )

    // The stations panel lists the real stations.
    await waitFor(
      () => {
        expect(screen.getAllByText(/Fuel Station$/).length).toBeGreaterThan(0)
      },
      { timeout: 30000 },
    )

    // The decision panel computed a real plan.
    await waitFor(
      () => {
        expect(screen.getByText('Recommended plan')).toBeInTheDocument()
      },
      { timeout: 30000 },
    )
  })

  it.skipIf(!LIVE_BACKEND)('shows the real depots and routes', async () => {
    render(<App />)

    // The depots panel lists the live depots by name.
    await waitFor(
      () => {
        expect(screen.getAllByText(/Depot$/).length).toBeGreaterThan(0)
      },
      { timeout: 30000 },
    )

    // The routes panel lists the live routes.
    await waitFor(
      () => {
        expect(screen.getAllByText(/→/).length).toBeGreaterThan(0)
      },
      { timeout: 30000 },
    )
  })

  it.skipIf(!LIVE_BACKEND)('never renders a number the backend did not send', async () => {
    // The live check is stricter than the fixture check: it re-scrapes every
    // number the live console rendered and confirms each one appears in the
    // live payloads.
    const payloads = await Promise.all(
      [
        '/api/metrics',
        '/api/depots',
        '/api/stations',
        '/api/routes',
        '/api/forecast/summary',
        '/api/anomalies',
        '/api/incidents',
        '/api/optimize',
      ].map(async (path) => {
        const response = await fetch(`${LIVE_BACKEND}${path}`, {
          method: path === '/api/optimize' ? 'POST' : 'GET',
          headers: { 'Content-Type': 'application/json' },
          body: path === '/api/optimize' ? '{}' : undefined,
        })
        return response.json()
      }),
    )

    const allowed = new Set<number>()
    const walk = (value: unknown) => {
      if (typeof value === 'number' && Number.isFinite(value)) {
        allowed.add(value)
        allowed.add(Number(value.toFixed(0)))
        allowed.add(Number(value.toFixed(1)))
        allowed.add(Number(value.toFixed(2)))
        allowed.add(Number((value * 100).toFixed(1)))
      } else if (Array.isArray(value)) {
        value.forEach(walk)
      } else if (value && typeof value === 'object') {
        Object.values(value).forEach(walk)
      }
    }
    payloads.forEach(walk)

    const { container } = render(<App />)
    await waitFor(
      () => {
        expect(screen.getByText('Recommended plan')).toBeInTheDocument()
      },
      { timeout: 30000 },
    )

    const leaves = container.querySelectorAll('*')
    const found: number[] = []
    for (const element of leaves) {
      if (element.children.length > 0) continue
      const matches = (element.textContent ?? '').match(/-?\d[\d,]*\.?\d*/g)
      if (!matches) continue
      for (const token of matches) {
        const value = Number(token.replace(/,/g, ''))
        if (Number.isFinite(value)) found.push(value)
      }
    }

    // The display constants the console is allowed to compute.
    ;[0, 1, 2, 3, 15, 24, 60, 96, 100].forEach((value) => allowed.add(value))

    const invented = found.filter((value) => !allowed.has(value))
    expect(invented, `Live console rendered unknown numbers: ${invented.join(', ')}`).toEqual([])
  })
})
