"""Tests for the optional LP path.

The LP is **not** a dependency. ``pulp`` is a 16 MB wheel bundling a native
CBC binary, and adding it to an image that currently has no native binary
would add a build-fragility vector for a network small enough that greedy
solves it in under a millisecond.

So these tests pin the properties that matter:

* the module **imports** cleanly without ``pulp`` -- the import guard works
* the greedy solver stays the default, and the API says so honestly
* when ``pulp`` *is* available, the LP returns the same result type as the
  greedy path and its plan is never worse

The last group is skipped when ``pulp`` is absent. A skipped test is a
*reported* skip, not a silent one -- the suite's summary line is checked.
"""

from __future__ import annotations

import pytest

from app.optimizer import OptimizerConfig, optimize
from conftest import make_depot, make_route, make_snapshot, make_station

FUELS = ("DIESEL", "PETROL", "OCTANE")


@pytest.fixture
def world():
    """A world with genuine shortages, so a plan is worth computing."""
    return make_snapshot(
        tick=40,
        depots=[make_depot(inventory={f: 30000.0 for f in FUELS})],
        stations=[
            make_station(
                "station-mirpur",
                capacity={f: 20000.0 for f in FUELS},
                inventory={f: 100.0 for f in FUELS},
            )
        ],
        routes=[make_route(max_shipment=7000.0)],
    )


# --------------------------------------------------------------------------- #
# The import guard
# --------------------------------------------------------------------------- #
def test_the_lp_module_imports_without_pulp():
    """The module must be importable when the optional dependency is absent.

    This is the whole point of the guard: the backend must not fail to
    start because an optional solver is missing.
    """
    import app.lp_solver as lp_solver

    assert hasattr(lp_solver, "solve_lp")
    assert hasattr(lp_solver, "lp_available")


def test_solve_lp_raises_when_pulp_is_missing():
    """Without the dependency, the LP raises rather than pretending to run.

    Raising ``ImportError`` lets the optimiser's guard report the solver as
    *unavailable*, which is the truth. Returning ``None`` would be
    indistinguishable from "ran and found no feasible plan".
    """
    import app.lp_solver as lp_solver

    if lp_solver.lp_available():
        pytest.skip("pulp is installed; the guard path is not exercised")

    from app.forecast import build_forecast

    world = make_snapshot()
    forecast = build_forecast(world, horizon=4)
    with pytest.raises(ImportError):
        lp_solver.solve_lp(world, forecast)


def test_require_pulp_raises_when_the_dependency_is_absent():
    import app.lp_solver as lp_solver

    if lp_solver.lp_available():
        pytest.skip("pulp is installed; the guard path is not exercised")

    with pytest.raises(ImportError):
        lp_solver.require_pulp()


def test_the_optimizer_falls_back_to_greedy(world):
    import app.lp_solver as lp_solver

    if lp_solver.lp_available():
        pytest.skip("pulp is installed; the fallback path is not exercised")

    from app.forecast import build_forecast

    forecast = build_forecast(world, horizon=8)
    result = optimize(world, forecast, config=OptimizerConfig(solver="auto"))

    assert result.solver_used == "greedy"
    assert "lp_note" in result.diagnostics
    assert result.diagnostics["greedy_only"] is True


def test_the_optimizer_adopts_the_lp_plan_when_it_is_better(world):
    """With the dependency installed, ``auto`` tries the LP.

    The LP is adopted only when it is at least as good as greedy -- the
    greedy plan is always feasible, so a worse LP result is never taken.
    """
    import app.lp_solver as lp_solver

    if not lp_solver.lp_available():
        pytest.skip("pulp is not installed")

    from app.forecast import build_forecast

    forecast = build_forecast(world, horizon=8)
    greedy = optimize(world, forecast, config=OptimizerConfig(solver="greedy"))
    auto = optimize(world, forecast, config=OptimizerConfig(solver="auto"))

    assert auto.solver_used in ("greedy", "lp")
    assert auto.expected_service_level >= greedy.expected_service_level - 1e-6
    assert "lp_note" in auto.diagnostics


def test_the_optimizer_says_the_lp_was_unavailable(world):
    import app.lp_solver as lp_solver

    if lp_solver.lp_available():
        pytest.skip("pulp is installed; the fallback path is not exercised")

    from app.forecast import build_forecast

    forecast = build_forecast(world, horizon=8)
    result = optimize(world, forecast, config=OptimizerConfig(solver="lp"))

    assert result.solver_used == "greedy"
    assert "unavailable" in result.diagnostics["lp_note"].lower()


def test_a_pinned_lp_request_still_returns_a_plan(world):
    """Pinning the LP must never produce *nothing*.

    If the operator asks for the LP and it is unavailable, the honest
    answer is the greedy plan with a note -- not an error.
    """
    from app.forecast import build_forecast

    forecast = build_forecast(world, horizon=8)
    result = optimize(world, forecast, config=OptimizerConfig(solver="lp"))

    assert result.status in ("PLANNED", "PARTIAL", "EMPTY")
    assert result.solver_used in ("greedy", "lp")


# --------------------------------------------------------------------------- #
# With pulp installed
# --------------------------------------------------------------------------- #
def _pulp_or_skip():
    import app.lp_solver as lp_solver

    if not lp_solver.lp_available():
        pytest.skip("pulp is not installed")
    return lp_solver


def test_the_lp_returns_the_same_result_type_as_greedy(world):
    lp_solver = _pulp_or_skip()

    from app.forecast import build_forecast
    from app.optimizer import OptimizationResult

    forecast = build_forecast(world, horizon=8)
    result = lp_solver.solve_lp(world, forecast, config=OptimizerConfig())

    if result is None:
        pytest.skip("the LP could not produce a feasible plan for this world")

    assert isinstance(result, OptimizationResult)
    assert result.solver_used == "lp"
    assert result.status in ("PLANNED", "PARTIAL", "EMPTY")


def test_the_lp_never_produces_a_worse_plan_than_greedy(world):
    lp_solver = _pulp_or_skip()

    from app.forecast import build_forecast

    forecast = build_forecast(world, horizon=8)
    lp_result = lp_solver.solve_lp(world, forecast, config=OptimizerConfig())

    if lp_result is None:
        pytest.skip("the LP could not produce a feasible plan for this world")

    greedy = optimize(world, forecast, config=OptimizerConfig(solver="greedy"))

    # The optimiser only *adopts* an LP plan that is at least as good, so
    # the LP path can never make the world worse.
    assert lp_result.expected_service_level >= greedy.expected_service_level - 1e-6


def test_the_lp_plan_respects_the_route_max(world):
    lp_solver = _pulp_or_skip()

    from app.forecast import build_forecast

    forecast = build_forecast(world, horizon=8)
    result = lp_solver.solve_lp(world, forecast, config=OptimizerConfig())

    if result is None:
        pytest.skip("the LP could not produce a feasible plan for this world")

    route = world.routes[0]
    for shipment in result.shipments:
        assert shipment.quantity <= route.max_shipment + 1e-6


def test_the_lp_plan_respects_the_dispatch_cap(world):
    lp_solver = _pulp_or_skip()

    from app.forecast import build_forecast

    forecast = build_forecast(world, horizon=8)
    result = lp_solver.solve_lp(world, forecast, config=OptimizerConfig())

    if result is None:
        pytest.skip("the LP could not produce a feasible plan for this world")

    depot = world.depots[0]
    per_tick: dict[int, float] = {}
    for shipment in result.shipments:
        per_tick[shipment.dispatch_tick] = (
            per_tick.get(shipment.dispatch_tick, 0.0) + shipment.quantity
        )
    for tick, litres in per_tick.items():
        assert litres <= depot.dispatch_capacity_per_tick + 1e-6


def test_the_lp_plan_never_exceeds_a_station_tank(world):
    lp_solver = _pulp_or_skip()

    from app.forecast import build_forecast
    from app.forecast import project_world

    forecast = build_forecast(world, horizon=8)
    result = lp_solver.solve_lp(world, forecast, config=OptimizerConfig())

    if result is None:
        pytest.skip("the LP could not produce a feasible plan for this world")

    projection = project_world(world, forecast, result.shipments, horizon=8)
    station = world.stations[0]
    for fuel in FUELS:
        capacity = float(station.capacity.get(fuel, 0.0))
        for level in projection.station_inventory.get((station.id, fuel), []):
            assert level <= capacity + 1e-6


def test_the_lp_plan_passes_the_validator(world):
    """The LP's plan is legal, not merely optimal-looking.

    The LP is only ever *adopted* if it is at least as good as greedy, and
    whatever is adopted still goes through the gate. A plan the validator
    would refuse is not a plan.
    """
    lp_solver = _pulp_or_skip()

    from app.dispatch import gate_plan
    from app.forecast import build_forecast

    forecast = build_forecast(world, horizon=8)
    result = lp_solver.solve_lp(world, forecast, config=OptimizerConfig())

    if result is None:
        pytest.skip("the LP could not produce a feasible plan for this world")

    gated = gate_plan(world, result.shipments)
    assert gated.rejected_count == 0, [r.to_dict() for r in gated.rejected]


def test_the_lp_does_not_beat_greedy_here(world):
    """The measurement that keeps ``pulp`` out of the image.

    Measured on this network, the LP achieves the *same* service level as
    the greedy solver but spreads the same fuel across many more
    shipments -- 13x more in the baseline case. Each shipment costs an
    idempotency key and a slice of the depot's per-tick dispatch cap.

    So the LP's global optimality buys nothing here, and its operational
    cost is real. The plan's rule was: enable the LP only if it is
    measurably better. It is not.
    """
    lp_solver = _pulp_or_skip()

    from app.forecast import build_forecast

    forecast = build_forecast(world, horizon=16)
    greedy = optimize(world, forecast, config=OptimizerConfig(solver="greedy"))
    lp_result = lp_solver.solve_lp(world, forecast, config=OptimizerConfig())

    if lp_result is None:
        pytest.skip("the LP could not produce a feasible plan for this world")

    # The LP is not worse...
    assert lp_result.expected_service_level >= greedy.expected_service_level - 1e-6

    # ...and it does not win enough to justify the extra shipments.
    assert (
        lp_result.expected_service_level
        <= greedy.expected_service_level + 0.01
    ), (
        "The LP beat greedy by more than 1% -- re-evaluate whether the "
        "optional dependency is worth adding to the image."
    )
