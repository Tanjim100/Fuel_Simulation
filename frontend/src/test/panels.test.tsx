/**
 * The panels.
 *
 * These tests assert the console's core promise: **what is on screen is what
 * the backend said.** A panel that computes its own figure, or rounds a value
 * into something the backend never claimed, is a bug — and these tests are
 * what catches it.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { BrokerHealth } from '../api/types'
import { AnomalyPanel, IncidentPanel } from '../components/AnomalyPanel'
import { DecisionPanel } from '../components/DecisionPanel'
import { DemandPanel, ForecastPanel } from '../components/ForecastPanel'
import { DepotPanel, RoutePanel, StationPanel } from '../components/NetworkPanel'
import { StaleBanner, TelemetryBar } from '../components/TelemetryBar'
import {
  anomalies,
  broker,
  depots,
  forecast,
  incidents,
  metrics,
  plan,
  routes,
  stations,
} from './fixtures'

describe('TelemetryBar', () => {
  it('shows the service level the backend reported', () => {
    render(
      <TelemetryBar
        health={null}
        broker={broker}
        metrics={metrics}
        stations={stations}
        routes={routes}
      />,
    )
    expect(screen.getByText('100.0%')).toBeInTheDocument()
  })

  it('counts the disrupted routes', () => {
    render(
      <TelemetryBar
        health={null}
        broker={broker}
        metrics={metrics}
        stations={stations}
        routes={routes}
      />,
    )
    // One route in the fixture is DISRUPTED.
    expect(screen.getByText('1', { selector: '.stat__value' })).toBeInTheDocument()
  })

  it('shows an em dash rather than a zero when there is no metrics reading', () => {
    render(
      <TelemetryBar
        health={null}
        broker={null}
        metrics={null}
        stations={[]}
        routes={[]}
      />,
    )
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })
})

describe('StaleBanner', () => {
  it('stays hidden when the data is fresh', () => {
    render(<StaleBanner broker={broker} />)
    expect(screen.queryByText('Stale data')).not.toBeInTheDocument()
  })

  it('warns when the snapshot is stale', () => {
    const stale = {
      ...broker,
      snapshot: { ...broker.snapshot, is_stale: true, age_seconds: 120 },
    }
    render(<StaleBanner broker={stale} />)
    expect(screen.getByText('Stale data')).toBeInTheDocument()
  })

  it('warns when the upstream is unreachable', () => {
    const unreachable: BrokerHealth = {
      ...broker,
      broker: { ...broker.broker, sse_reachable: false, mode: 'polling' },
    }
    render(<StaleBanner broker={unreachable} />)
    expect(screen.getByText('Upstream unreachable')).toBeInTheDocument()
  })
})

describe('DepotPanel', () => {
  it('names each depot', () => {
    render(<DepotPanel depots={depots} />)
    expect(screen.getByText('Gazipur Depot')).toBeInTheDocument()
    expect(screen.getByText('Patiya Depot')).toBeInTheDocument()
  })

  it('shows the dispatch headroom', () => {
    render(<DepotPanel depots={depots} />)
    expect(screen.getByText('12,000 L')).toBeInTheDocument()
  })

  it('shows the per-tick cap so the headroom has context', () => {
    render(<DepotPanel depots={depots} />)
    expect(screen.getByText('cap 12,000 L/tick')).toBeInTheDocument()
  })
})

describe('StationPanel', () => {
  it('names each station', () => {
    render(<StationPanel stations={stations} />)
    expect(screen.getByText('Mirpur Fuel Station')).toBeInTheDocument()
  })

  it('marks a single-connected station', () => {
    render(<StationPanel stations={stations} />)
    expect(screen.getByText(/single-connected/)).toBeInTheDocument()
  })

  it('shows the demand multiplier when it is elevated', () => {
    render(<StationPanel stations={stations} />)
    // The fixture's tongi station has a multiplier of 1.8.
    expect(screen.getByText('×1.8')).toBeInTheDocument()
  })

  it('selects a station when clicked', async () => {
    const onSelect = vi.fn()
    render(<StationPanel stations={stations} onSelect={onSelect} />)
    await userEvent.click(screen.getByText('Mirpur Fuel Station'))
    expect(onSelect).toHaveBeenCalledWith('station-mirpur')
  })
})

describe('RoutePanel', () => {
  it('shows a route as depot to station', () => {
    render(<RoutePanel routes={routes} />)
    expect(screen.getByText('gazipur → mirpur')).toBeInTheDocument()
  })

  it('badges a disrupted route as disrupted', () => {
    render(<RoutePanel routes={routes} />)
    expect(screen.getByText('DISRUPTED')).toBeInTheDocument()
  })

  it('counts the disrupted routes in the header', () => {
    render(<RoutePanel routes={routes} />)
    expect(screen.getByText('1 disrupted')).toBeInTheDocument()
  })

  it('warns that a single-connected station cannot be rerouted', () => {
    render(<RoutePanel routes={routes} />)
    expect(screen.getByText(/cannot be rerouted/)).toBeInTheDocument()
  })
})

describe('ForecastPanel', () => {
  it('shows the total forecast demand', () => {
    render(<ForecastPanel summary={forecast} risks={[]} />)
    expect(screen.getByText('10,302 L')).toBeInTheDocument()
  })

  it('shows the stockout probability the backend computed', () => {
    render(<ForecastPanel summary={forecast} risks={[]} />)
    expect(screen.getByText('82%')).toBeInTheDocument()
  })

  it('shows the ticks to stockout', () => {
    render(<ForecastPanel summary={forecast} risks={[]} />)
    expect(screen.getByText('2 ticks')).toBeInTheDocument()
  })
})

describe('DemandPanel', () => {
  it('breaks the demand down by fuel', () => {
    render(<DemandPanel summary={forecast} coverageTicks={16} />)
    expect(screen.getByText('DIESEL')).toBeInTheDocument()
  })
})

describe('AnomalyPanel', () => {
  it('shows the anomaly title', () => {
    render(<AnomalyPanel anomalies={anomalies} />)
    expect(screen.getByText('Route route-gazipur-mirpur is DISRUPTED')).toBeInTheDocument()
  })

  it('shows the recommended action', () => {
    render(<AnomalyPanel anomalies={anomalies} />)
    expect(screen.getByText(/Reroute through the alternative depot/)).toBeInTheDocument()
  })

  it('shows the backend metrics behind the anomaly', () => {
    render(<AnomalyPanel anomalies={anomalies} />)
    expect(screen.getByText('transit_ticks')).toBeInTheDocument()
  })

  it('says so plainly when there is nothing anomalous', () => {
    render(<AnomalyPanel anomalies={[]} />)
    expect(screen.getByText('Nothing anomalous')).toBeInTheDocument()
  })
})

describe('IncidentPanel', () => {
  it('shows the incident summary', () => {
    render(<IncidentPanel incidents={incidents} />)
    expect(screen.getByText(/1 anomaly on route-gazipur-mirpur/)).toBeInTheDocument()
  })
})

describe('DecisionPanel', () => {
  const noop = () => {}

  it('shows the backend headline verbatim', () => {
    render(
      <DecisionPanel
        plan={plan}
        execution={null}
        busy={false}
        error={null}
        onExecute={noop}
        onRecompute={noop}
      />,
    )
    expect(
      screen.getByText('Ship 7000 L of DIESEL from Patiya Depot to Mirpur Fuel Station now.'),
    ).toBeInTheDocument()
  })

  it('shows the verdict the backend gave', () => {
    render(
      <DecisionPanel
        plan={plan}
        execution={null}
        busy={false}
        error={null}
        onExecute={noop}
        onRecompute={noop}
      />,
    )
    expect(screen.getByText('RECOMMENDED')).toBeInTheDocument()
  })

  it('publishes the declared numbers so the prose can be audited', () => {
    render(
      <DecisionPanel
        plan={plan}
        execution={null}
        busy={false}
        error={null}
        onExecute={noop}
        onRecompute={noop}
      />,
    )
    expect(screen.getByText('total_litres')).toBeInTheDocument()
    expect(screen.getByText('service_level')).toBeInTheDocument()
  })

  it('separates the current-tick shipments from the advisory ones', () => {
    render(
      <DecisionPanel
        plan={plan}
        execution={null}
        busy={false}
        error={null}
        onExecute={noop}
        onRecompute={noop}
      />,
    )
    expect(screen.getByText('Advisory — future ticks')).toBeInTheDocument()
    expect(screen.getByText(/not written now/)).toBeInTheDocument()
  })

  it('does not dispatch until the operator confirms', async () => {
    const onExecute = vi.fn()
    render(
      <DecisionPanel
        plan={plan}
        execution={null}
        busy={false}
        error={null}
        onExecute={onExecute}
        onRecompute={noop}
      />,
    )

    // The first click arms the action, and must not dispatch.
    await userEvent.click(screen.getByText('Dispatch current-tick shipments'))
    expect(onExecute).not.toHaveBeenCalled()

    // The second click confirms it.
    await userEvent.click(screen.getByText('Confirm dispatch'))
    expect(onExecute).toHaveBeenCalledTimes(1)
  })

  it('lets the operator back out of the confirmation', async () => {
    const onExecute = vi.fn()
    render(
      <DecisionPanel
        plan={plan}
        execution={null}
        busy={false}
        error={null}
        onExecute={onExecute}
        onRecompute={noop}
      />,
    )
    await userEvent.click(screen.getByText('Dispatch current-tick shipments'))
    await userEvent.click(screen.getByText('Cancel'))
    expect(onExecute).not.toHaveBeenCalled()
  })

  it('disables the dispatch button when there is nothing to dispatch', () => {
    const emptyPlan = {
      ...plan,
      optimization: { ...plan.optimization, shipments_now: [], shipments_later: [] },
      gated: { ...plan.gated, executable_now: 0, deferred_to_future_ticks: 0 },
    }
    render(
      <DecisionPanel
        plan={emptyPlan}
        execution={null}
        busy={false}
        error={null}
        onExecute={noop}
        onRecompute={noop}
      />,
    )
    expect(screen.getByText('Nothing to dispatch')).toBeDisabled()
  })

  it('reports what a previous dispatch did', () => {
    const execution = {
      ...plan,
      created: [{ id: 1 }],
      replayed: [],
      failed: [],
      total_litres: 7000,
    }
    render(
      <DecisionPanel
        plan={plan}
        execution={execution}
        busy={false}
        error={null}
        onExecute={noop}
        onRecompute={noop}
      />,
    )
    // The execution summary shows the created count and the dispatched volume.
    const dispatched = screen.getByText('Dispatched').parentElement
    expect(dispatched).toHaveTextContent('7,000 L')
    expect(screen.getByText('Created').parentElement).toHaveTextContent('1')
    expect(screen.getByText('Failed').parentElement).toHaveTextContent('0')
  })
})
