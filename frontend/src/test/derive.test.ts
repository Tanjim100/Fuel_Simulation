/**
 * The derived readings.
 *
 * These tests pin the rule that matters most for the console's credibility:
 * **a derived reading may only ever be a pure function of numbers the backend
 * sent.** No estimation, no smoothing, no guessing.
 */

import { describe, expect, it } from 'vitest'
import { stations } from './fixtures'
import {
  closedStations,
  coverageDays,
  depotTotal,
  dispatchLevel,
  fillLevel,
  fillRatio,
  severityLevel,
  severityRank,
  serviceLevel,
  stationTotal,
  statusLevel,
  streamLevel,
  tightFuels,
  verdictLevel,
  worstLevel,
} from '../lib/derive'

describe('fillRatio', () => {
  it('is the inventory as a fraction of capacity', () => {
    expect(fillRatio(5000, 10000)).toBe(0.5)
  })

  it('is clamped to 1 — an over-full tank reads as full, not as 140%', () => {
    expect(fillRatio(14000, 10000)).toBe(1)
  })

  it('is clamped to 0 — a negative reading reads as empty', () => {
    expect(fillRatio(-50, 10000)).toBe(0)
  })

  it('treats an unknown capacity as empty rather than infinitely full', () => {
    // A station whose capacity the backend did not report cannot be called
    // healthy. Reporting "full" would be the dangerous answer.
    expect(fillRatio(5000, 0)).toBe(0)
    expect(fillRatio(5000, Number.NaN)).toBe(0)
  })
})

describe('fillLevel', () => {
  it('calls a nearly-empty tank dangerous', () => {
    expect(fillLevel(0.05)).toBe('danger')
  })

  it('calls a quarter-full tank a warning', () => {
    expect(fillLevel(0.25)).toBe('warn')
  })

  it('calls a well-stocked tank fine', () => {
    expect(fillLevel(0.8)).toBe('ok')
  })

  it('puts the boundaries exactly where it says', () => {
    expect(fillLevel(0.15)).toBe('warn')
    expect(fillLevel(0.35)).toBe('ok')
  })
})

describe('dispatchLevel', () => {
  it('calls an exhausted depot dangerous', () => {
    expect(dispatchLevel(0, 12000)).toBe('danger')
  })

  it('calls a nearly-exhausted depot a warning', () => {
    expect(dispatchLevel(2000, 12000)).toBe('warn')
  })

  it('calls a depot with headroom fine', () => {
    expect(dispatchLevel(12000, 12000)).toBe('ok')
  })
})

describe('streamLevel', () => {
  it('treats a live SSE stream as fine', () => {
    expect(streamLevel('sse', true)).toBe('ok')
  })

  it('treats polling with a reachable stream as a warning, not a failure', () => {
    // The simulator may simply be paused. That is degraded, not broken.
    expect(streamLevel('polling', true)).toBe('warn')
  })

  it('treats polling with an unreachable stream as dangerous', () => {
    expect(streamLevel('polling', false)).toBe('danger')
  })

  it('treats a stopped broker as dangerous', () => {
    expect(streamLevel('stopped', false)).toBe('danger')
  })
})

describe('serviceLevel', () => {
  it('calls a perfect service level fine', () => {
    expect(serviceLevel(1)).toBe('ok')
  })

  it('calls a small shortfall a warning', () => {
    expect(serviceLevel(0.95)).toBe('warn')
  })

  it('calls a large shortfall dangerous', () => {
    expect(serviceLevel(0.5)).toBe('danger')
  })

  it('treats an unknown service level as a warning, never as fine', () => {
    // Absence of data is not evidence of health.
    expect(serviceLevel(null)).toBe('warn')
    expect(serviceLevel(undefined)).toBe('warn')
    expect(serviceLevel(Number.NaN)).toBe('warn')
  })
})

describe('worstLevel', () => {
  it('is dominated by danger', () => {
    expect(worstLevel(['ok', 'warn', 'danger'])).toBe('danger')
  })

  it('is warn when there is no danger', () => {
    expect(worstLevel(['ok', 'warn'])).toBe('warn')
  })

  it('is ok when everything is ok', () => {
    expect(worstLevel(['ok', 'ok'])).toBe('ok')
  })

  it('ignores the presentational tones', () => {
    // A muted label must not make a summary tile look degraded.
    expect(worstLevel(['ok', 'neutral', 'dim'])).toBe('ok')
  })
})

describe('severity ordering', () => {
  it('ranks CRITICAL above HIGH above MEDIUM above LOW', () => {
    expect(severityRank('CRITICAL')).toBeLessThan(severityRank('HIGH'))
    expect(severityRank('HIGH')).toBeLessThan(severityRank('MEDIUM'))
    expect(severityRank('MEDIUM')).toBeLessThan(severityRank('LOW'))
  })

  it('sorts an unknown severity last', () => {
    expect(severityRank('SOMETHING_NEW')).toBeGreaterThan(severityRank('LOW'))
  })

  it('colours CRITICAL and HIGH as danger', () => {
    expect(severityLevel('CRITICAL')).toBe('danger')
    expect(severityLevel('HIGH')).toBe('danger')
  })

  it('colours MEDIUM as a warning and LOW as fine', () => {
    expect(severityLevel('MEDIUM')).toBe('warn')
    expect(severityLevel('LOW')).toBe('ok')
  })
})

describe('verdictLevel', () => {
  it('colours RECOMMENDED as fine', () => {
    expect(verdictLevel('RECOMMENDED')).toBe('ok')
  })

  it('colours CAUTION as a warning', () => {
    expect(verdictLevel('CAUTION')).toBe('warn')
  })

  it('colours BLOCKED as dangerous', () => {
    expect(verdictLevel('BLOCKED')).toBe('danger')
  })
})

describe('statusLevel', () => {
  it('treats OPEN and AVAILABLE as fine', () => {
    expect(statusLevel('OPEN')).toBe('ok')
    expect(statusLevel('AVAILABLE')).toBe('ok')
  })

  it('treats CONSTRAINED as a warning, not a failure', () => {
    // A constrained depot is still dispatchable. Showing it red would be
    // wrong.
    expect(statusLevel('CONSTRAINED')).toBe('warn')
  })

  it('treats DISRUPTED, CLOSED and OUTAGE as dangerous', () => {
    expect(statusLevel('DISRUPTED')).toBe('danger')
    expect(statusLevel('CLOSED')).toBe('danger')
    expect(statusLevel('OUTAGE')).toBe('danger')
  })

  it('treats an unknown status as a warning rather than assuming fine', () => {
    expect(statusLevel('SOMETHING_NEW')).toBe('warn')
  })
})

describe('totals', () => {
  it('sums a depot inventory across every fuel', () => {
    expect(depotTotal({ DIESEL: 57000, PETROL: 45000, OCTANE: 26000 })).toBe(128000)
  })

  it('sums a station inventory across every fuel', () => {
    expect(stationTotal({ DIESEL: 11573.776, PETROL: 8465.376, OCTANE: 4720.086 })).toBe(
      24759.238,
    )
  })

  it('treats a missing fuel as zero, not as NaN', () => {
    expect(depotTotal({ DIESEL: 100 })).toBe(100)
  })
})

describe('tightFuels', () => {
  const base = {
    id: 's',
    name: 's',
    region_id: 'r',
    status: 'OPEN',
    demand_profile: 'urban_high',
    demand_multiplier: 1,
    capacity: { DIESEL: 100, PETROL: 100, OCTANE: 100 },
    inventory: { DIESEL: 100, PETROL: 100, OCTANE: 100 },
    free_space: { DIESEL: 0, PETROL: 50, OCTANE: 50 },
    inbound_in_flight: {},
    spill_exposure: {},
    route_count: 1,
    is_single_connected: false,
  }

  it('names a fuel with no free space', () => {
    expect(tightFuels(base)).toEqual(['DIESEL'])
  })

  it('does not name a fuel with headroom', () => {
    expect(
      tightFuels({
        ...base,
        inventory: { DIESEL: 10, PETROL: 10, OCTANE: 10 },
        free_space: { DIESEL: 90, PETROL: 90, OCTANE: 90 },
      }),
    ).toEqual([])
  })

  it('names a fuel the backend did not report free space for', () => {
    // An omitted fuel cannot be assumed to have room.
    expect(tightFuels({ ...base, free_space: { PETROL: 50, OCTANE: 50 } })).toEqual(['DIESEL'])
  })
})

describe('coverageDays', () => {
  it('converts ticks to days at 15 minutes per tick', () => {
    // 96 ticks x 15 minutes = 1440 minutes = 1 day.
    expect(coverageDays(96, 15)).toBe(1)
  })

  it('returns null for zero coverage rather than zero days', () => {
    // "0 days of fuel" and "we cannot tell" are different claims.
    expect(coverageDays(0, 15)).toBeNull()
    expect(coverageDays(-1, 15)).toBeNull()
  })
})

describe('filtering helpers', () => {
  it('finds closed stations', () => {
    const open = stations[0]!
    const closed = { ...open, id: 'station-closed', status: 'CLOSED' }
    expect(closedStations([open, closed])).toHaveLength(1)
  })
})
