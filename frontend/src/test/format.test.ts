/**
 * The formatting layer.
 *
 * The rule: a format function *renders* a number the backend sent. It never
 * derives one. These tests pin the edge cases that would otherwise show up
 * as "—" (an unknown) when a real zero is on screen.
 */

import { describe, expect, it } from 'vitest'
import {
  clockTime,
  count,
  duration,
  humanise,
  litres,
  percent,
  percentOfHundred,
  shortId,
  signed,
  ticks,
} from '../lib/format'

describe('litres', () => {
  it('renders a whole number without decimals', () => {
    expect(litres(1000)).toBe('1,000')
  })

  it('keeps one decimal below 100 L', () => {
    expect(litres(64.476)).toBe('64.5')
  })

  it('renders zero as zero, not as an em dash', () => {
    // Zero is a reading. Only *unknown* is an em dash.
    expect(litres(0)).toBe('0')
  })

  it('renders an unknown as an em dash', () => {
    expect(litres(null)).toBe('—')
    expect(litres(undefined)).toBe('—')
    expect(litres(Number.NaN)).toBe('—')
  })
})

describe('percent', () => {
  it('scales a 0..1 fraction', () => {
    expect(percent(0.9157)).toBe('91.6%')
  })

  it('renders zero as 0.0%', () => {
    expect(percent(0)).toBe('0.0%')
  })

  it('renders one as 100.0%', () => {
    expect(percent(1)).toBe('100.0%')
  })

  it('renders an unknown as an em dash', () => {
    expect(percent(null)).toBe('—')
  })
})

describe('percentOfHundred', () => {
  it('formats an already-scaled percentage', () => {
    expect(percentOfHundred(100)).toBe('100.0%')
    expect(percentOfHundred(94.4)).toBe('94.4%')
  })
})

describe('count', () => {
  it('groups thousands', () => {
    expect(count(4392)).toBe('4,392')
  })

  it('renders zero as zero', () => {
    expect(count(0)).toBe('0')
  })
})

describe('signed', () => {
  it('marks a positive number with a plus', () => {
    expect(signed(2.5)).toBe('+2.5')
  })

  it('leaves a negative number alone', () => {
    expect(signed(-2.5)).toBe('-2.5')
  })

  it('leaves zero alone', () => {
    expect(signed(0)).toBe('0.0')
  })
})

describe('duration', () => {
  it('renders seconds below a minute', () => {
    expect(duration(45)).toBe('45s')
  })

  it('renders minutes below an hour', () => {
    expect(duration(120)).toBe('2m')
  })

  it('renders hours and minutes', () => {
    expect(duration(3 * 3600 + 5 * 60)).toBe('3h 05m')
  })

  it('renders days', () => {
    expect(duration(50 * 3600)).toBe('2d 2h')
  })

  it('never renders a negative duration', () => {
    expect(duration(-10)).toBe('0s')
  })
})

describe('ticks', () => {
  it('singularises one tick', () => {
    expect(ticks(1)).toBe('1 tick')
  })

  it('pluralises the rest', () => {
    expect(ticks(16)).toBe('16 ticks')
  })

  it('renders an unknown as an em dash', () => {
    expect(ticks(null)).toBe('—')
  })
})

describe('shortId', () => {
  it('drops the entity prefix', () => {
    expect(shortId('station-mirpur')).toBe('mirpur')
  })

  it('leaves a prefix-less id alone', () => {
    expect(shortId('mirpur')).toBe('mirpur')
  })
})

describe('humanise', () => {
  it('title-cases a machine token', () => {
    expect(humanise('urban_high')).toBe('Urban High')
  })

  it('turns dashes into spaces', () => {
    expect(humanise('route-disruption')).toBe('Route Disruption')
  })
})

describe('clockTime', () => {
  it('renders a valid timestamp', () => {
    expect(clockTime('2026-01-01T08:00:00Z')).not.toBe('—')
  })

  it('renders an invalid timestamp as an em dash', () => {
    expect(clockTime('not a time')).toBe('—')
  })

  it('renders a missing timestamp as an em dash', () => {
    expect(clockTime(null)).toBe('—')
  })
})
