/**
 * The autopilot bar — the console's switch between "a human decides" and
 * "the system decides".
 *
 * It sits directly above the Action Card, because the Action Card is the
 * dispatch surface: the card's button changes with the mode, and the card
 * shows what the autopilot dispatched. The bar's only job is to *be* the
 * switch, and to say where the system currently stands.
 *
 * Two modes, and each one says plainly what it does:
 *
 *   - **Manual** — nothing happens without an operator. The card tells the
 *     operator when there is something to dispatch.
 *   - **Autopilot** — the system dispatches on its own, once per tick, and
 *     the card shows what it dispatched.
 *
 * The dispatch figures live on the card, not here. That is the merge: one
 * place says the mode, one place says what was dispatched, and the card
 * carries both because it knows the mode.
 *
 * **Every number here is the backend's.** The bar renders `GET
 * /api/autopilot` and nothing else — no derivation, no local arithmetic. If
 * the autopilot panel needed a derived figure it would have to live in
 * `lib/derive.ts` and be added to the no-invented-numbers allow-list, and
 * the autopilot does not need one: the backend already publishes every
 * figure it wants shown.
 */

import type { AutopilotMode, AutopilotStatus } from '../api/types'
import { Badge } from './common'
import { count } from '../lib/format'

const MODES: { id: AutopilotMode; label: string; hint: string }[] = [
  {
    id: 'off',
    label: 'Manual',
    hint: 'The console tells you when there is something to dispatch, and you dispatch it.',
  },
  {
    id: 'live',
    label: 'Autopilot',
    hint: 'The system dispatches what the gate accepts, once per tick.',
  },
]

/** A decision token, and how it should read. */
function decisionLevel(decision: string | null): 'ok' | 'warn' | 'neutral' | 'danger' {
  if (decision === 'dispatched') return 'ok'
  if (decision === 'error') return 'danger'
  if (decision === 'breaker_open' || decision === 'quarantined') return 'warn'
  return 'neutral'
}

function decisionText(decision: string | null): string {
  if (!decision) return 'no cycles yet'
  return decision.replace(/_/g, ' ')
}

export function AutopilotBar({
  autopilot,
  busy,
  error,
  onChange,
}: {
  autopilot: AutopilotStatus | null
  busy: boolean
  error: string | null
  onChange: (mode: AutopilotMode) => void
}) {
  const mode: AutopilotMode = autopilot?.mode ?? 'off'
  const live = mode === 'live'

  return (
    <section className="autopilot" aria-label="Autopilot">
      <div className="autopilot__head">
        <div className="autopilot__modes" role="group" aria-label="Dispatch mode">
          {MODES.map((option) => (
            <button
              key={option.id}
              type="button"
              className={`autopilot__mode${mode === option.id ? ' autopilot__mode--on' : ''}`}
              onClick={() => onChange(option.id)}
              disabled={busy}
              aria-pressed={mode === option.id}
              title={option.hint}
            >
              {option.label}
            </button>
          ))}
        </div>

        <div className="autopilot__state">
          <Badge level={live ? 'accent' : 'neutral'}>
            {live ? 'Autopilot live' : 'Manual dispatch'}
          </Badge>
          {autopilot?.running ? <Badge level="ok">running</Badge> : null}
          {autopilot?.last_escalated ? <Badge level="warn">escalated</Badge> : null}
          {autopilot?.last_decision ? (
            <Badge level={decisionLevel(autopilot.last_decision)}>
              {decisionText(autopilot.last_decision)}
            </Badge>
          ) : null}
          {autopilot ? (
            <span className="autopilot__cycles">
              {count(autopilot.cycle)} {autopilot.cycle === 1 ? 'cycle' : 'cycles'}
            </span>
          ) : null}
        </div>
      </div>

      {live ? (
        <p className="autopilot__note">
          Live — the autopilot re-plans against the live world every tick and dispatches what the
          gate accepts. The card below shows what it dispatched; you can still dispatch by hand.
        </p>
      ) : (
        <p className="autopilot__note">
          Manual — the system is not dispatching. When there is something to dispatch, the card
          below asks you to.
        </p>
      )}

      {autopilot?.quarantine_until_tick !== null && autopilot?.quarantine_until_tick !== undefined ? (
        <p className="tiny faint">
          Quarantined until tick {count(autopilot.quarantine_until_tick)} — the last dispatch was
          rejected, so this tick is skipped.
        </p>
      ) : null}

      {autopilot?.last_error ? (
        <p className="tiny" style={{ color: 'var(--danger)' }}>
          {autopilot.last_error}
        </p>
      ) : null}

      {error ? (
        <p className="tiny" style={{ color: 'var(--danger)' }}>
          {error}
        </p>
      ) : null}
    </section>
  )
}
