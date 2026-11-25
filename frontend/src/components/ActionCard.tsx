/**
 * The action card — the one thing the operator came to do.
 *
 * Everything else on this screen is secondary to this card. It answers, in
 * order:
 *
 *   1. **What should I do?**   — the headline, in one line.
 *   2. **Where and what?**     — the route and the fuel.
 *   3. **Why?**               — the backend's own explanation, in its words.
 *   4. **Is it safe?**        — the gate's verdict, and the figures behind it.
 *   5. **Do it.**             — one button. Two clicks.
 *
 * The panel's two rules survive the redesign unchanged:
 *
 *   1. It never writes without an explicit click. `POST /api/optimize` is
 *      read-only and runs automatically; `POST /api/optimize/execute` only
 *      runs on the confirmed button.
 *   2. It never claims more than the backend said. The headline, the litre
 *      counts and the verdicts are the backend's, verbatim.
 */

import { useState } from 'react'
import type { ExecuteResponse, OptimizeResponse, PlannedShipment } from '../api/types'
import { Badge, Empty, NumberChips } from './common'
import { Disclosure } from './Disclosure'
import { count, litres, percent, shortId, ticks } from '../lib/format'
import type { HealthLevel } from '../lib/derive'
import { verdictLevel } from '../lib/derive'

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
        {shipment.fuel_type} · dispatch tick {shipment.dispatch_tick} · arrive tick{' '}
        {shipment.arrival_tick}
      </div>
      <div className="shipment__meta">
        <Badge level={riskLevel}>{shipment.risk}</Badge> <Badge level="neutral">{shipment.binding}</Badge>
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

export function ActionCard({
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
  const [detailsOpen, setDetailsOpen] = useState(false)

  const optimization = plan?.optimization
  const gated = plan?.gated
  const explanation = plan?.explanation

  const now = optimization?.shipments_now ?? []
  const later = optimization?.shipments_later ?? []
  const rejected = gated?.rejected ?? 0

  // The headline shipment is the one the operator is about to ship. It is
  // the backend's ordering (the optimiser's `sequence`), not a re-sort.
  const lead = now[0]

  return (
    <section className="hero">
      <div className="hero__head">
        <div className="hero__verdict">
          <Badge level={explanation ? verdictLevel(explanation.verdict) : 'neutral'}>
            {explanation?.verdict ?? optimization?.status ?? 'computing'}
          </Badge>
          {rejected > 0 ? <Badge level="danger">{rejected} rejected</Badge> : null}
        </div>
        <button className="btn btn--ghost" onClick={onRecompute} disabled={busy}>
          {busy ? 'Working…' : 'Recompute'}
        </button>
      </div>

      {!plan ? (
        <div className="hero__body">
          <Empty title="No plan yet">Computing the first plan…</Empty>
        </div>
      ) : (
        <>
          <div className="hero__body">
            {/*
              * The headline. The backend's explanation, in its own words —
              * the console does not paraphrase a decision it did not make.
              */}
            <h2 className="hero__headline">{explanation?.headline ?? 'No shipments planned'}</h2>

            {lead ? (
              <p className="hero__route">
                {shortId(lead.source_depot_id)} → {shortId(lead.destination_station_id)} ·{' '}
                {lead.fuel_type}
              </p>
            ) : (
              <p className="hero__route">Nothing falls on the current tick.</p>
            )}

            {explanation?.details?.length ? (
              <p className="hero__why">{explanation.details[0]}</p>
            ) : null}

            <div className="hero__figures">
              <div className="fact">
                <span className="fact__label">Planned volume</span>
                <span className="fact__value fact__value--dim">
                  {litres(optimization?.total_litres ?? 0)} L
                </span>
              </div>
              <div className="fact">
                <span className="fact__label">Projected service level</span>
                <span className="fact__value fact__value--dim">
                  {percent(optimization?.expected_service_level ?? 0)}
                </span>
              </div>
              <div className="fact">
                <span className="fact__label">Shippable now</span>
                <span className="fact__value fact__value--dim">
                  {count(gated?.executable_now ?? now.length)}
                </span>
              </div>
              <div className="fact">
                <span className="fact__label">Gate</span>
                <span className="fact__value fact__value--dim">
                  {gated ? `${gated.accepted} ok / ${gated.rejected} no` : '—'}
                </span>
              </div>
            </div>
          </div>

          <div className="hero__actions">
            {confirming ? (
              <>
                <button
                  className="btn btn--primary btn--lg"
                  onClick={() => {
                    setConfirming(false)
                    onExecute()
                  }}
                  disabled={busy}
                >
                  {busy ? 'Dispatching…' : `Confirm — dispatch ${litres(optimization?.total_litres ?? 0)} L`}
                </button>
                <button className="btn" onClick={() => setConfirming(false)} disabled={busy}>
                  Cancel
                </button>
              </>
            ) : (
              <button
                className="btn btn--primary btn--lg"
                onClick={() => setConfirming(true)}
                disabled={busy || now.length === 0}
              >
                {now.length === 0
                  ? 'Nothing to dispatch'
                  : `Dispatch ${litres(optimization?.total_litres ?? 0)} L`}
              </button>
            )}

            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => setDetailsOpen(!detailsOpen)}
              aria-expanded={detailsOpen}
            >
              {detailsOpen ? 'Hide details' : 'Details'}
            </button>

            {error ? (
              <span className="hint" style={{ color: 'var(--danger)' }}>
                {error}
              </span>
            ) : null}
          </div>

          {confirming ? (
            <div className="hero__confirm">
              This writes the current-tick shipments to the simulator. Future-tick shipments are
              advisory and are never written early.
            </div>
          ) : null}

          {/*
            * The evidence, one click away. The explanation's full detail
            * list, its declared numbers, its risks and its next actions, the
            * shipments themselves, and the result of the last dispatch.
            */}
          <Disclosure
            label="Why this plan"
            openLabel="Hide the reasoning"
            open={detailsOpen}
            onOpenChange={setDetailsOpen}
          >
            {explanation && explanation.details.length > 1 ? (
              <ul className="explanation__list">
                {explanation.details.slice(1).map((detail, index) => (
                  <li key={index}>{detail}</li>
                ))}
              </ul>
            ) : null}

            {explanation?.risks?.length ? (
              <>
                <p className="section-label" style={{ marginTop: 20 }}>
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
                <p className="section-label" style={{ marginTop: 20 }}>
                  Suggested next actions
                </p>
                <ul className="explanation__list">
                  {explanation.next_actions.map((action, index) => (
                    <li key={index}>{action}</li>
                  ))}
                </ul>
              </>
            ) : null}

            {explanation ? <NumberChips numbers={explanation.numbers} /> : null}

            {now.length > 0 ? (
              <>
                <p className="section-label" style={{ marginTop: 20 }}>
                  Shipping now
                </p>
                {now.map((shipment) => (
                  <ShipmentRow
                    key={`${shipment.route_id}-${shipment.fuel_type}-${shipment.sequence}`}
                    shipment={shipment}
                    advisory={false}
                  />
                ))}
              </>
            ) : null}

            {later.length > 0 ? (
              <>
                <p className="section-label" style={{ marginTop: 20 }}>
                  Advisory — future ticks, not written now
                </p>
                {later.map((shipment) => (
                  <ShipmentRow
                    key={`later-${shipment.route_id}-${shipment.fuel_type}-${shipment.sequence}`}
                    shipment={shipment}
                    advisory
                  />
                ))}
              </>
            ) : null}

            {execution ? (
              <>
                <p className="section-label" style={{ marginTop: 20 }}>
                  Last dispatch
                </p>
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
              </>
            ) : null}
          </Disclosure>
        </>
      )}
    </section>
  )
}

export { ticks }
