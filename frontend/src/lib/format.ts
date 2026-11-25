/**
 * Number formatting for an operator console.
 *
 * Every function here takes a number the backend sent and renders it. None of
 * them compute a business figure — a ratio, a percentage, a severity — because
 * a display function that derives a number is a display function that can
 * disagree with the backend. Derivation happens in `derive.ts` and is
 * covered by tests; formatting only renders.
 */

/** Litres, with thousands separators and no decimals above 100 L. */
export function litres(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  if (Math.abs(value) >= 1000) {
    return value.toLocaleString(undefined, { maximumFractionDigits: 0 })
  }
  if (Math.abs(value) >= 100) {
    return value.toLocaleString(undefined, { maximumFractionDigits: 0 })
  }
  return value.toLocaleString(undefined, { maximumFractionDigits: 1 })
}

/** A percentage from a 0..1 fraction, with one decimal. */
export function percent(fraction: number | null | undefined, digits = 1): string {
  if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) return '—'
  return `${(fraction * 100).toFixed(digits)}%`
}

/** A percentage from an already-scaled 0..100 number, with one decimal. */
export function percentOfHundred(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value.toFixed(digits)}%`
}

/** Whole numbers with thousands separators. */
export function count(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return value.toLocaleString()
}

/** A signed number, for deltas. */
export function signed(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const rounded = value.toFixed(digits)
  return value > 0 ? `+${rounded}` : rounded
}

/** Seconds as a compact human duration ("4s", "12m", "3h 05m"). */
export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—'
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  const minutes = Math.floor(s / 60)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  if (hours < 24) return `${hours}h ${String(rest).padStart(2, '0')}m`
  const days = Math.floor(hours / 24)
  return `${days}d ${hours % 24}h`
}

/** A tick count, or "—" when there is none. */
export function ticks(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value} tick${value === 1 ? '' : 's'}`
}

/** Trim a machine id down to its human part ("station-mirpur" → "mirpur"). */
export function shortId(id: string): string {
  const dash = id.indexOf('-')
  return dash === -1 ? id : id.slice(dash + 1)
}

/** Title-case a machine token ("urban_high" → "Urban High"). */
export function humanise(token: string): string {
  return token
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

/** A clock time from an ISO timestamp, in the viewer's locale. */
export function clockTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}
