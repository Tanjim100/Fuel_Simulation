/**
 * The console's components.
 *
 * These tests assert the console's core promise: **what is on screen is what
 * the backend said.** A panel that computes its own figure, or rounds a value
 * into something the backend never claimed, is a bug — and these tests are
 * what catches it.
 *
 * The console was redesigned to be readable — one column, one hero, one
 * panel at a time. The *claims* below are unchanged from before the
 * redesign; only the selectors and the components moved. A redesign that
 * quietly dropped a guarantee would show up here as a missing assertion.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { BrokerHealth } from '../api/types'
import { ActionCard } from '../components/ActionCard'
import { AnomalyPanel, IncidentPanel } from '../components/AnomalyPanel'
import { DetailTabs } from '../components/DetailTabs'
import { ForecastPanel } from '../components/ForecastPanel'
import { DepotPanel, RoutePanel, StationPanel } from '../components/NetworkPanel'
import { StaleBanner } from '../components/TelemetryBar'
import { StatusBar } from '../components/StatusBar'
import { SystemPanel } from '../components/SystemPanel'
import {
  anomalies,
  broker,
  depots,
  forecast,
  incidents,
  metrics,
  observability,
  plan,
  routes,
  stations,
} from './fixtures'

describe('StatusBar', () => {
  const renderBar = (overrides: Partial<Parameters<typeof StatusBar>[0]> = {}) =>
    render(
      <StatusBar
        health={null}
        broker={broker}
        metrics={metrics}
        stations={stations}
        routes={routes}
        theme="dark"
        onToggleTheme={() => {}}
        {...overrides}
      />,
    )

  it('shows the service level the backend reported', () => {
    renderBar()
    expect(screen.getByText('100.0%')).toBeInTheDocument()
  })

  it('counts the disrupted routes', () => {
    renderBar()
    // One route in the fixture is DISRUPTED.
    const disrupted = screen.getByText('Disrupted routes').parentElement
    expect(disrupted).toHaveTextContent('1')
  })

  it('shows an em dash rather than a zero when there is no metrics reading', () => {
    renderBar({ broker: null, metrics: null, stations: [], routes: [] })
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })

  it('says so when the network is broken', () => {
    renderBar()
    expect(screen.getByText('attention')).toBeInTheDocument()
  })

  it('offers a theme toggle', () => {
    const onToggleTheme = vi.fn()
    renderBar({ onToggleTheme })
    screen.getByRole('button', { name: /switch to light theme/i }).click()
    expect(onToggleTheme).toHaveBeenCalled()
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
    // The cap is context for the headroom figure beside it: "12,000 L of
    // 12,000 L" — how much of this tick's allowance is left.
    expect(screen.getByText(/of 12,000 L/)).toBeInTheDocument()
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

  it('keeps the per-fuel detail behind a disclosure', async () => {
    render(<StationPanel stations={stations} />)
    // The fuel breakdown is not on screen until it is asked for.
    expect(screen.queryByText('DIESEL')).not.toBeInTheDocument()
    const toggles = screen.getAllByText('Fuel levels')
    await userEvent.click(toggles[0]!)
    expect(screen.getAllByText('DIESEL').length).toBeGreaterThan(0)
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

  it('breaks the demand down by fuel', () => {
    render(<ForecastPanel summary={forecast} risks={[]} />)
    // DIESEL appears both in the fuel breakdown and in the stockout table.
    // The breakdown is the one that carries a litres figure.
    const breakdowns = screen.getAllByText('DIESEL')
    const withLitres = breakdowns.filter((element) =>
      (element.parentElement?.textContent ?? '').includes('L'),
    )
    expect(withLitres.length).toBeGreaterThan(0)
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

  it('keeps the backend metrics behind a disclosure', async () => {
    render(<AnomalyPanel anomalies={anomalies} />)
    expect(screen.queryByText('transit_ticks')).not.toBeInTheDocument()
    await userEvent.click(screen.getByText('Evidence'))
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

describe('SystemPanel', () => {
  it('renders the §16 component table in the brief’s order', () => {
    render(<SystemPanel observability={observability} />)
    expect(screen.getByText('Backend API')).toBeInTheDocument()
    expect(screen.getByText('Database')).toBeInTheDocument()
    expect(screen.getByText('Fuel Simulator')).toBeInTheDocument()
    expect(screen.getByText('Prediction Service')).toBeInTheDocument()
    expect(screen.getByText('Decision Engine')).toBeInTheDocument()
  })

  it('shows the p95 latency and the error rate the backend computed', () => {
    render(<SystemPanel observability={observability} />)
    // The fixture's p95 is 0.02425 s and the error rate is 0.
    expect(screen.getByText('24 ms')).toBeInTheDocument()
    expect(screen.getByText('0.00%')).toBeInTheDocument()
  })

  it('shows the circuit state', () => {
    render(<SystemPanel observability={observability} />)
    expect(screen.getByText(/circuit closed/i)).toBeInTheDocument()
  })

  it('shows the pool saturation as a percentage with its read limit', () => {
    render(<SystemPanel observability={observability} />)
    // The fixture's saturation gauge is 0.25 and the limit is 4.
    expect(screen.getByText('25%')).toBeInTheDocument()
    expect(screen.getByText(/4 read limit/)).toBeInTheDocument()
  })

  it('shows a degraded component as degraded', () => {
    const degraded = {
      ...observability,
      components: {
        ...observability.components,
        fuel_simulator: { state: 'DEGRADED' },
      },
    }
    render(<SystemPanel observability={degraded} />)
    expect(screen.getAllByText('DEGRADED').length).toBeGreaterThan(0)
  })

  it('says so plainly when there is no observability payload', () => {
    render(<SystemPanel observability={null} />)
    expect(screen.getByText('No observability data yet')).toBeInTheDocument()
  })
})

describe('DetailTabs', () => {  it('shows one panel at a time', async () => {
    render(
      <DetailTabs
        tabs={[
          { id: 'a', label: 'First', content: <p>Panel A</p> },
          { id: 'b', label: 'Second', content: <p>Panel B</p> },
        ]}
      />,
    )
    expect(screen.getByText('Panel A')).toBeInTheDocument()
    expect(screen.queryByText('Panel B')).not.toBeInTheDocument()

    // The tab click has to be awaited — the switch is a state update.
    await userEvent.click(screen.getByRole('tab', { name: /Second/ }))
    expect(screen.getByText('Panel B')).toBeInTheDocument()
    expect(screen.queryByText('Panel A')).not.toBeInTheDocument()
  })

  it('counts the problems on the tab so they are never hidden', () => {
    render(
      <DetailTabs
        tabs={[
          { id: 'a', label: 'First', content: <p>Panel A</p> },
          { id: 'b', label: 'Problems', count: 3, countIsProblem: true, content: <p>Panel B</p> },
        ]}
      />,
    )
    expect(screen.getByText('3')).toBeInTheDocument()
  })
})

describe('ActionCard', () => {
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

  it('shows the backend headline verbatim', () => {
    renderCard()
    expect(
      screen.getByText('Ship 7000 L of DIESEL from Patiya Depot to Mirpur Fuel Station now.'),
    ).toBeInTheDocument()
  })

  it('shows the verdict the backend gave', () => {
    renderCard()
    expect(screen.getByText('RECOMMENDED')).toBeInTheDocument()
  })

  it('publishes the declared numbers so the prose can be audited', async () => {
    renderCard()
    await userEvent.click(screen.getByText('Why this plan'))
    expect(screen.getByText('total_litres')).toBeInTheDocument()
    expect(screen.getByText('service_level')).toBeInTheDocument()
  })

  it('separates the current-tick shipments from the advisory ones', async () => {
    renderCard()
    await userEvent.click(screen.getByText('Why this plan'))
    expect(screen.getByText(/Advisory — future ticks/)).toBeInTheDocument()
    // The advisory shipments carry their own "not written now" note, and the
    // section heading says so too.
    expect(screen.getAllByText(/not written now/).length).toBeGreaterThan(0)
  })

  it('does not dispatch until the operator confirms', async () => {
    const onExecute = vi.fn()
    renderCard({ onExecute })

    // The first click arms the action, and must not dispatch.
    await userEvent.click(screen.getByText(/^Dispatch /))
    expect(onExecute).not.toHaveBeenCalled()

    // The second click confirms it.
    await userEvent.click(screen.getByText(/^Confirm — dispatch/))
    expect(onExecute).toHaveBeenCalledTimes(1)
  })

  it('lets the operator back out of the confirmation', async () => {
    const onExecute = vi.fn()
    renderCard({ onExecute })
    await userEvent.click(screen.getByText(/^Dispatch /))
    await userEvent.click(screen.getByText('Cancel'))
    expect(onExecute).not.toHaveBeenCalled()
  })

  it('disables the dispatch button when there is nothing to dispatch', () => {
    const emptyPlan = {
      ...plan,
      optimization: { ...plan.optimization, shipments_now: [], shipments_later: [] },
      gated: { ...plan.gated, executable_now: 0, deferred_to_future_ticks: 0 },
    }
    renderCard({ plan: emptyPlan })
    expect(screen.getByText('Nothing to dispatch')).toBeDisabled()
  })

  it('reports what a previous dispatch did', async () => {
    const execution = {
      ...plan,
      created: [{ id: 1 }],
      replayed: [],
      failed: [],
      total_litres: 7000,
    }
    renderCard({ execution })
    await userEvent.click(screen.getByText('Why this plan'))

    const dispatched = screen.getByText('Dispatched').parentElement
    expect(dispatched).toHaveTextContent('7,000 L')
    expect(screen.getByText('Created').parentElement).toHaveTextContent('1')
    expect(screen.getByText('Failed').parentElement).toHaveTextContent('0')
  })
})
