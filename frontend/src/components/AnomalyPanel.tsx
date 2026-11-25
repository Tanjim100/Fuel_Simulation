/**
 * The anomaly panel — what is going wrong, and what to do about it.
 *
 * The detectors are the backend's. Each anomaly carries a title, a detail, the
 * metrics behind it, and a recommended action — all backend-authored. The
 * console's job is to make them legible and ordered by urgency.
 */

import type { Anomaly, Incident } from '../api/types'
import { Badge, Empty, Panel } from './common'
import { count, litres } from '../lib/format'
import { severityLevel, severityRank } from '../lib/derive'

export function AnomalyPanel({
  anomalies,
  onExplain,
}: {
  anomalies: Anomaly[]
  onExplain?: (anomaly: Anomaly) => void
}) {
  const sorted = [...anomalies].sort(
    (a, b) => severityRank(a.severity) - severityRank(b.severity) || a.tick - b.tick,
  )

  return (
    <Panel
      title="Anomalies"
      action={
        anomalies.length > 0 ? (
          <Badge level={severityLevel(sorted[0]?.severity ?? 'LOW')}>{count(anomalies.length)}</Badge>
        ) : (
          <Badge level="ok">none</Badge>
        )
      }
      flush
    >
      {sorted.length === 0 ? (
        <Empty title="Nothing anomalous">
          No demand spike, inventory drop, bottleneck or stockout risk detected.
        </Empty>
      ) : (
        sorted.map((anomaly) => (
          <div className="shipment" key={anomaly.id}>
            <div className="shipment__head">
              <span className="shipment__route">{anomaly.title}</span>
              <Badge level={severityLevel(anomaly.severity)}>{anomaly.severity}</Badge>
            </div>
            <div className="shipment__meta">
              {anomaly.kind} · {anomaly.entity_id} · tick {anomaly.tick}
            </div>
            <p className="shipment__why">{anomaly.detail}</p>
            {anomaly.recommended_action ? (
              <p className="shipment__why" style={{ color: 'var(--accent)' }}>
                → {anomaly.recommended_action}
              </p>
            ) : null}
            {Object.keys(anomaly.metrics).length > 0 ? (
              <div style={{ marginTop: 8 }}>
                {Object.entries(anomaly.metrics).map(([key, value]) => (
                  <span className="number-chip" key={key} style={{ marginRight: 6 }}>
                    <span className="number-chip__key">{key}</span> {value}
                  </span>
                ))}
              </div>
            ) : null}
            {onExplain ? (
              <button
                className="btn"
                style={{ marginTop: 10 }}
                onClick={() => onExplain(anomaly)}
              >
                Explain
              </button>
            ) : null}
          </div>
        ))
      )}
    </Panel>
  )
}

export function IncidentPanel({ incidents }: { incidents: Incident[] }) {
  const sorted = [...incidents].sort(
    (a, b) => severityRank(a.severity) - severityRank(b.severity) || b.start_tick - a.start_tick,
  )

  return (
    <Panel
      title="Incidents"
      action={incidents.length > 0 ? <Badge level="warn">{count(incidents.length)}</Badge> : null}
      flush
    >
      {sorted.length === 0 ? (
        <Empty title="No open incidents" />
      ) : (
        sorted.map((incident) => (
          <div className="shipment" key={incident.id}>
            <div className="shipment__head">
              <span className="shipment__route">{incident.title}</span>
              <Badge level={severityLevel(incident.severity)}>{incident.severity}</Badge>
            </div>
            <div className="shipment__meta">
              ticks {incident.start_tick}–{incident.end_tick} · {incident.kinds.join(', ')}
            </div>
            <p className="shipment__why">{incident.summary}</p>
            {incident.explanation ? (
              <>
                <p className="shipment__why" style={{ color: 'var(--text)' }}>
                  {incident.explanation.headline}
                </p>
                {incident.explanation.next_actions.length > 0 ? (
                  <ul className="explanation__list">
                    {incident.explanation.next_actions.map((action, index) => (
                      <li key={index}>{action}</li>
                    ))}
                  </ul>
                ) : null}
              </>
            ) : null}
          </div>
        ))
      )}
    </Panel>
  )
}

export function JournalPanel({
  entries,
}: {
  entries: Array<{
    key: string
    status: string
    depot: string
    station: string
    fuel: string
    quantity: number
    created_tick: number
  }>
}) {
  return (
    <Panel title="Recent dispatches" flush>
      {entries.length === 0 ? (
        <Empty title="No dispatches journalled yet" />
      ) : (
        <div className="scroll-y">
          <table className="table">
            <thead>
              <tr>
                <th>Route</th>
                <th>Status</th>
                <th className="num">Quantity</th>
                <th className="num">Tick</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.key}>
                  <td>
                    <div className="entity">
                      <span className="entity__name">
                        {entry.depot} → {entry.station}
                      </span>
                      <span className="entity__meta">
                        {entry.fuel} · {entry.key}
                      </span>
                    </div>
                  </td>
                  <td>
                    <Badge
                      level={
                        entry.status === 'CREATED'
                          ? 'ok'
                          : entry.status === 'FAILED'
                            ? 'danger'
                            : 'warn'
                      }
                    >
                      {entry.status}
                    </Badge>
                  </td>
                  <td className="num">{litres(entry.quantity)} L</td>
                  <td className="num">{entry.created_tick}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}
