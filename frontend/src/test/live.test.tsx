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

/**
 * The live backend's URL, read from the environment.
 *
 * `process` is declared here rather than pulled in from `@types/node`: the
 * console's browser bundle has no business depending on Node's type
 * definitions, and the Docker build's `tsc` step type-checks this file
 * without a Node types package installed.
 */
declare const process: { env: Record<string, string | undefined> }

const LIVE_BACKEND = process.env.LIVE_BACKEND

// The live test talks to a running stack over the network. The default 5s
// budget is not enough for a full render plus nine parallel reads, so this
// file opts into a longer one.
vi.setConfig({ testTimeout: 60_000, hookTimeout: 60_000 })

let originalFetch: typeof globalThis.fetch

/**
 * Every payload the console itself received, keyed by endpoint.
 *
 * The simulator is a *live* world: it ticks between requests. So the live
 * invariant test cannot snapshot the payloads separately and then render —
 * the snapshot would be from a different tick than the render, and every
 * figure that moved in between would look "invented".
 *
 * Instead the test **captures the responses the console actually fetched**,
 * by wrapping `fetch`. The allow-list is then built from exactly those
 * bodies. A figure the console invented would not be in them.
 *
 * Responses are kept for the whole file: the console polls every four
 * seconds, so several ticks' worth accumulate. The allow-list is the union
 * across all of them — a number the backend sent at any point during the
 * render is a number the backend sent.
 */
const captured = new Map<string, unknown[]>()

/** Record a captured body's numbers into the allow-list. */
function collect(value: unknown, into: Set<number>) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    into.add(value)
    into.add(Number(value.toFixed(0)))
    into.add(Number(value.toFixed(1)))
    into.add(Number(value.toFixed(2)))
    // A percentage of a 0..1 fraction, as the console renders it.
    into.add(Number((value * 100).toFixed(1)))
    into.add(Number((value * 100).toFixed(0)))
  } else if (Array.isArray(value)) {
    value.forEach((item) => collect(item, into))
  } else if (value && typeof value === 'object') {
    Object.values(value).forEach((item) => collect(item, into))
  }
}

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

    if (url.startsWith('/api/') && !url.includes('/events/stream')) {
      const path = url.split('?')[0] ?? url

      return originalFetch(`${LIVE_BACKEND}${url}`, init).then((response) => {
        // Record the body the console is about to render from. The response
        // is cloned *before* anything reads it, so the console still gets an
        // unread stream of its own.
        void response
          .clone()
          .json()
          .then((body) => {
            const existing = captured.get(path) ?? []
            existing.push(body)
            captured.set(path, existing)
          })
          .catch(() => {
            /* a non-JSON response is not part of the world state */
          })

        return response
      })
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

    // The status bar populates from the real metrics endpoint. Wait for
    // the *tick number*, not the label — the label renders before the data
    // arrives.
    await waitFor(
      () => {
        const bar = document.querySelector('.statusbar')
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

    // The action card computed a real plan. The redesigned console has no
    // "Recommended plan" heading — the hero *is* the plan. Wait for the
    // hero's headline, which the backend always sends once a plan exists.
    //
    // The headline is the right signal rather than the verdict badge: the
    // verdict depends on the world's state (RECOMMENDED, CAUTION, BLOCKED,
    // INFORMATIONAL…), so asserting a fixed set of strings would make the
    // test fail whenever the live world happened to be in a different state.
    await waitFor(
      () => {
        const headline = document.querySelector('.hero__headline')
        expect(headline?.textContent ?? '').not.toBe('')
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
    //
    // The allow-list is built from the responses **the console itself
    // received**, captured by the fetch wrapper above. Snapshotting the
    // endpoints separately would race the live simulator: it ticks between
    // the snapshot and the render, so every figure that moved would look
    // invented when it is merely from a later tick.
    const { container } = render(<App />)
    await waitFor(
      () => {
        const headline = document.querySelector('.hero__headline')
        expect(headline?.textContent ?? '').not.toBe('')
      },
      { timeout: 30000 },
    )

    // Let one full poll cycle land, so the captured set covers the same
    // world the rendered DOM came from.
    await new Promise((resolve) => setTimeout(resolve, 1000))

    const allowed = new Set<number>()
    for (const bodies of captured.values()) {
      for (const body of bodies) collect(body, allowed)
    }

    // The display constants the console is allowed to compute.
    ;[0, 1, 2, 3, 15, 24, 60, 96, 100].forEach((value) => allowed.add(value))

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

    const invented = found.filter((value) => !allowed.has(value))
    expect(invented, `Live console rendered unknown numbers: ${invented.join(', ')}`).toEqual([])
  })
})
