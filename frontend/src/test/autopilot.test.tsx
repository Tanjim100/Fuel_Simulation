/**
 * The autopilot bar, and the Action Card as the merged dispatch surface.
 *
 * Three promises are checked here:
 *
 *   1. **The bar reports the backend's truth.** Every figure it shows comes
 *      from `GET /api/autopilot`, and the mode buttons say plainly what
 *      each mode does.
 *   2. **The card speaks for both modes.** Manual, it asks the operator to
 *      dispatch (`Dispatch needed — 7,000 L`); automatic, it shows what the
 *      system dispatched (`Last dispatch: tick 61 · 4,200 L · created 1`).
 *   3. **The manual button is always available.** The operator can always
 *      overrule an automatic decision -- but when the autopilot is live the
 *      button says `Dispatch … anyway` and the confirmation warns that the
 *      autopilot may also dispatch on the same tick.
 *
 * The third promise is the one the user asked for by name: *"also provide
 * button to manually control dispatch while auto pilot is off"*. The
 * implementation makes it available in every mode, because a human must be
 * able to overrule the system -- and it changes the *label*, not the
 * availability.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ActionCard } from '../components/ActionCard'
import { AutopilotBar } from '../components/AutopilotBar'
import { autopilot, autopilotLiveDispatched, plan } from './fixtures'

describe('AutopilotBar', () => {
  const renderBar = (
    overrides: Partial<Parameters<typeof AutopilotBar>[0]> = {},
  ) =>
    render(
      <AutopilotBar
        autopilot={autopilot}
        busy={false}
        error={null}
        onChange={() => {}}
        {...overrides}
      />,
    )

  it('shows both modes', () => {
    renderBar()
    expect(screen.getByRole('button', { name: 'Manual' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Autopilot' })).toBeInTheDocument()
  })

  it('marks the current mode as pressed', () => {
    renderBar()
    expect(screen.getByRole('button', { name: 'Manual' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByRole('button', { name: 'Autopilot' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  })

  it('says it is manual when the autopilot is off', () => {
    renderBar()
    expect(screen.getByText('Manual dispatch')).toBeInTheDocument()
  })

  it('asks the backend to change the mode', async () => {
    const onChange = vi.fn()
    renderBar({ onChange })
    await userEvent.click(screen.getByRole('button', { name: 'Autopilot' }))
    expect(onChange).toHaveBeenCalledWith('live')
  })

  it('explains that manual mode leaves the dispatch to the operator', () => {
    renderBar()
    expect(screen.getByText(/the system is not dispatching/i)).toBeInTheDocument()
  })

  it('explains that live mode dispatches on its own', () => {
    renderBar({ autopilot: { ...autopilot, mode: 'live', enabled: true } })
    expect(screen.getByText(/dispatches what the gate accepts/)).toBeInTheDocument()
  })

  it('shows the backend’s last decision', () => {
    renderBar({ autopilot: autopilotLiveDispatched })
    expect(screen.getAllByText('dispatched').length).toBeGreaterThan(0)
    expect(screen.getByText('12 cycles')).toBeInTheDocument()
  })

  it('shows a failed dispatch in the danger colour', () => {
    renderBar({
      autopilot: {
        ...autopilot,
        mode: 'live',
        enabled: true,
        last_decision: 'error',
        last_error: '2 shipment(s) were rejected by the simulator.',
      },
    })
    expect(
      screen.getByText('2 shipment(s) were rejected by the simulator.'),
    ).toBeInTheDocument()
  })

  it('shows the quarantine when a tick was skipped', () => {
    renderBar({
      autopilot: {
        ...autopilot,
        mode: 'live',
        enabled: true,
        quarantine_until_tick: 74,
      },
    })
    expect(screen.getByText(/Quarantined until tick 74/)).toBeInTheDocument()
  })
})

describe('ActionCard as the merged dispatch surface', () => {
  const noop = () => {}

  const renderCard = (overrides: Partial<Parameters<typeof ActionCard>[0]> = {}) =>
    render(
      <ActionCard
        plan={plan}
        execution={null}
        busy={false}
        error={null}
        onExecute={noop}
        onRecompute={noop}
        {...overrides}
      />,
    )

  it('asks the operator to dispatch when the autopilot is off', () => {
    renderCard({ autopilot })
    expect(screen.getByText(/^Dispatch needed — 7,000 L$/)).toBeInTheDocument()
  })

  it('says "anyway" when the autopilot is live', () => {
    renderCard({ autopilot: { ...autopilot, mode: 'live', enabled: true } })
    expect(screen.getByText(/^Dispatch 7,000 L anyway$/)).toBeInTheDocument()
  })

  it('shows what the autopilot dispatched', () => {
    renderCard({ autopilot: autopilotLiveDispatched })
    expect(
      screen.getByText(/Last dispatch: tick 61 · 4,200 L · created 1/),
    ).toBeInTheDocument()
  })

  it('does not claim an autopilot dispatch when nothing was dispatched', () => {
    renderCard({ autopilot: { ...autopilot, mode: 'live', enabled: true } })
    expect(screen.queryByText(/Last dispatch:/)).not.toBeInTheDocument()
  })

  it('warns in the confirmation that the autopilot may also dispatch', async () => {
    renderCard({ autopilot: { ...autopilot, mode: 'live', enabled: true } })
    await userEvent.click(screen.getByText(/^Dispatch 7,000 L anyway$/))
    expect(screen.getByText(/autopilot is live/i)).toBeInTheDocument()
  })

  it('does not warn about the autopilot when it is off', async () => {
    renderCard({ autopilot })
    await userEvent.click(screen.getByText(/^Dispatch needed — 7,000 L$/))
    expect(screen.queryByText(/autopilot is live/i)).not.toBeInTheDocument()
  })

  it('still dispatches on the second click when the autopilot is live', async () => {
    const onExecute = vi.fn()
    renderCard({ autopilot: { ...autopilot, mode: 'live', enabled: true }, onExecute })

    await userEvent.click(screen.getByText(/^Dispatch 7,000 L anyway$/))
    expect(onExecute).not.toHaveBeenCalled()

    await userEvent.click(screen.getByText(/^Confirm — dispatch/))
    expect(onExecute).toHaveBeenCalledTimes(1)
  })

  it('works with no autopilot state at all', () => {
    // The autopilot being unavailable must not take the manual button with
    // it. Dispatch is the one thing the operator came to do.
    renderCard()
    expect(screen.getByText(/^Dispatch needed — 7,000 L$/)).toBeInTheDocument()
  })
})
