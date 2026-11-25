/**
 * The stale-data banner.
 *
 * The backend reports whether its snapshot is fresh. A console that shows
 * numbers without saying how old they are is a console that can lie. So the
 * banner is not decoration — it is the freshness contract, made visible.
 *
 * It lives alone in this file now. The old `TelemetryBar` — ten statistics
 * in a strip across the top — became `StatusBar`, which shows four facts.
 */

import type { BrokerHealth } from '../api/types'
import { duration } from '../lib/format'

export function StaleBanner({ broker }: { broker: BrokerHealth | null }) {
  if (!broker) return null

  const { snapshot, broker: b } = broker
  const isStale = snapshot.is_stale
  const unreachable = !b.sse_reachable

  if (!isStale && !unreachable && !b.resync_required) return null

  const level = isStale || unreachable ? 'danger' : 'warn'

  let title = 'The displayed data may be out of date'
  let body = ''

  if (isStale) {
    title = 'Stale data'
    body = `The world snapshot is ${duration(snapshot.age_seconds)} old and the upstream has not confirmed it. Figures below describe the last known state, not the current one.`
  } else if (unreachable) {
    title = 'Upstream unreachable'
    body = `The backend cannot reach the simulator over the event stream. It is polling instead. Figures below are as of ${duration(snapshot.age_seconds)} ago.`
  } else {
    title = 'Resync required'
    body = 'The backend missed events and is re-fetching state. Figures below are as of the last confirmed snapshot.'
  }

  return (
    <div className={`banner banner--${level}`} role="status">
      <div className="banner__text">
        <p className="banner__title">{title}</p>
        <p className="banner__body">{body}</p>
      </div>
    </div>
  )
}
