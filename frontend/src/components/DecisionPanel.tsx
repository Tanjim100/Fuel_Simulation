/**
 * The decision-support column — where the operator actually decides.
 *
 * The panel shows the optimiser's plan, the gate's verdict, the deterministic
 * explanation, and then **stops**. The write button is the operator's, and
 * pressing it is the only thing that dispatches fuel.
 *
 * Two rules the panel enforces on itself:
 *
 * 1. It never writes without an explicit click. `POST /api/optimize` is
 *    read-only and runs automatically; `POST /api/optimize/execute` only runs
 *    on the button.
 * 2. It never claims more than the backend said. The headline, the litre
 *    counts and the verdicts are the backend's, verbatim.
 */

import { useState } from 'react'
import type { ExecuteResponse, OptimizeResponse, PlannedShipment } from '../api/types'
import { Badge, Empty, NumberChips, Panel } from './common'
import { count, litres, percent, shortId } from '../lib/format'
import type { HealthLevel } from '../lib/derive'
import { severityLevel, verdictLevel } from '../lib/derive'

function ShipmentRow({ shipment, advisory }: { shipment: PlannedShipment; advisory: boolean }) {
  // A shipment's risk band decides its colour. Anything the optimiser did not
  // flag is shown muted rather than green — an unflagged shipment is not a
  // *good* shipment, it is an ordinary one.
  const riskLevel: HealthLevel =
    shipment.risk === 'SPILL_RISK' || shipment.risk === 'LATE_ARRIVAL' ? 'warn' : 'neutral'

  return (
    <div className="shipment">
      <div className="shipment__head">
        <span className="shipment__route">
          {shortId(shipment.source_depot_id)} → {shortId(shipment.destination_station_id)}
        </span>
        <span className="shipment__qty">{litres(shipment.quantity)} L</span>
      </div>
      <div className="shipment__meta">
        {shipment.fuel_type} · {shipment.route_id} · dispatch tick {shipment.dispatch_tick} ·
        arrive tick {shipment.arrival_tick}
      </div>
      <div className="shipment__meta">
        <Badge level={riskLevel}>{shipment.risk}</Badge>{' '}
        <Badge level="neutral">{shipment.binding}</Badge>
      </div>
      {shipment.rationale ? <p className="shipment__why">{shipment.rationale}</p> : null}
      {advisory ? (
        <p className="tiny faint" style={{ marginTop: 6, marginBottom: 0 }}>
          Advisory — recomputed next cycle, not written now.
        </p>
      ) : null}
    </div>
  )
}

export function DecisionPanel({
  plan,
  execution,
  busy,
  error,
  onExecute,
  onRecompute,
}: {
  plan: OptimizeResponse | null
  execution: ExecuteResponse | null
  busy: boolean
  error: string | null
  onExecute: () => void
  onRecompute: () => void
}) {
  const [confirming, setConfirming] = useState(false)
  const optimization = plan?.optimization
  const gated = plan?.gated
  const explanation = plan?.explanation

  const now = optimization?.shipments_now ?? []
  const later = optimization?.shipments_later ?? []
  const rejected = gated?.rejected ?? 0

  return (
    <>
      <Panel
        title="Recommended plan"
        action={
          <button className="btn" onClick={onRecompute} disabled={busy}>
            {busy ? 'Working…' : 'Recompute'}
          </button>
        }
        flush
      >
        {!plan ? (
          <Empty title="No plan yet">Computing the first plan…</Empty>
        ) : (
          <>
            <div style={{ padding: '12px 16px', borderBottom: '1px solid var(--border)' }}>
              <div className="btn-row" style={{ marginBottom: 12 }}>
                <Badge level={explanation ? verdictLevel(explanation.verdict) : 'neutral'}>
                  {explanation?.verdict ?? optimization?.status}
                </Badge>
                <Badge level="neutral">{optimization?.solver_used}</Badge>
                <Badge level="neutral">horizon {plan.horizon} ticks</Badge>
                {rejected > 0 ? <Badge level="danger">{rejected} rejected</Badge> : null}
              </div>

              {explanation ? (
                <p className="explanation__headline">{explanation.headline}</p>
              ) : null}

              {explanation && explanation.details.length > 0 ? (
                <ul className="explanation__list">
                  {explanation.details.map((detail, index) => (
                    <li key={index}>{detail}</li>
                  ))}
                </ul>
              ) : null}

              {explanation ? <NumberChips numbers={explanation.numbers} /> : null}

              {explanation?.risks?.length ? (
                <>
                  <p className="tiny faint" style={{ marginTop: 12, marginBottom: 4 }}>
                    Risks
                  </p>
                  <ul className="explanation__list">
                    {explanation.risks.map((risk, index) => (
                      <li key={index}>{risk}</li>
                    ))}
                  </ul>
                </>
              ) : null}

              {explanation?.next_actions?.length ? (
                <>
                  <p className="tiny faint" style={{ marginTop: 12, marginBottom: 4 }}>
                    Suggested next actions
                  </p>
                  <ul className="explanation__list">
                    {explanation.next_actions.map((action, index) => (
                      <li key={index}>{action}</li>
                    ))}
                  </ul>
                </>
              ) : null}
            </div>

            <div className="scroll-y">
              {now.length === 0 ? (
                <Empty title="Nothing to ship now">
                  No shipment falls on the current tick. The advisory plan below is recomputed
                  every cycle.
                </Empty>
              ) : (
                now.map((shipment) => (
                  <ShipmentRow
                    key={`${shipment.route_id}-${shipment.fuel_type}-${shipment.sequence}`}
                    shipment={shipment}
                    advisory={false}
                  />
                ))
              )}
            </div>

            {later.length > 0 ? (
              <>
                <div
                  style={{
                    padding: '8px 16px',
                    borderTop: '1px solid var(--border)',
                    borderBottom: '1px solid var(--border)',
                    background: 'var(--bg-raised)',
                  }}
                >
                  <span className="panel__title">Advisory — future ticks</span>
                </div>
                <div className="scroll-y">
                  {later.map((shipment) => (
                    <ShipmentRow
                      key={`later-${shipment.route_id}-${shipment.fuel_type}-${shipment.sequence}`}
                      shipment={shipment}
                      advisory
                    />
                  ))}
                </div>
              </>
            ) : null}
          </>
        )}
      </Panel>

      <Panel title="Dispatch">
        <div className="kv">
          <span className="kv__key">Executable now</span>
          <span className="kv__value">
            {count(gated?.executable_now ?? now.length)} shipment(s)
          </span>
        </div>
        <div className="kv">
          <span className="kv__key">Deferred to future ticks</span>
          <span className="kv__value">{count(gated?.deferred_to_future_ticks ?? later.length)}</span>
        </div>
        <div className="kv">
          <span className="kv__key">Gate verdict</span>
          <span className="kv__value">
            {gated ? `${gated.accepted} accepted / ${gated.rejected} rejected` : '—'}
          </span>
        </div>
        <div className="kv">
          <span className="kv__key">Planned volume</span>
          <span className="kv__value">{litres(optimization?.total_litres ?? 0)} L</span>
        </div>
        <div className="kv">
          <span className="kv__key">Projected service level</span>
          <span className="kv__value">{percent(optimization?.expected_service_level ?? 0)}</span>
        </div>

        {execution ? (
          <div style={{ marginTop: 12 }}>
            <div className="kv">
              <span className="kv__key">Created</span>
              <span className="kv__value">{count(execution.created.length)}</span>
            </div>
            <div className="kv">
              <span className="kv__key">Replayed</span>
              <span className="kv__value">{count(execution.replayed.length)}</span>
            </div>
            <div className="kv">
              <span className="kv__key">Failed</span>
              <span className="kv__value">{count(execution.failed.length)}</span>
            </div>
            <div className="kv">
              <span className="kv__key">Dispatched</span>
              <span className="kv__value">{litres(execution.total_litres)} L</span>
            </div>
          </div>
        ) : null}

        {error ? (
          <p className="hint" style={{ color: 'var(--danger)', marginTop: 12 }}>
            {error}
          </p>
        ) : null}

        <div style={{ marginTop: 16 }}>
          {confirming ? (
            <>
              <p className="hint" style={{ marginTop: 0 }}>
                This writes the current-tick shipments to the simulator. Future-tick shipments
                are advisory and are never written early.
              </p>
              <div className="btn-row" style={{ marginTop: 8 }}>
                <button
                  className="btn btn--primary btn--block"
                  onClick={() => {
                    setConfirming(false)
                    onExecute()
                  }}
                  disabled={busy}
                >
                  {busy ? 'Dispatching…' : 'Confirm dispatch'}
                </button>
                <button className="btn" onClick={() => setConfirming(false)} disabled={busy}>
                  Cancel
                </button>
              </div>
            </>
          ) : (
            <button
              className="btn btn--primary btn--block"
              onClick={() => setConfirming(true)}
              disabled={busy || now.length === 0}
            >
              {now.length === 0 ? 'Nothing to dispatch' : 'Dispatch current-tick shipments'}
            </button>
          )}
        </div>
      </Panel>
    </>
  )
}

export { severityLevel }
