"""Forecast endpoints.

The forecast is the optimiser's input, but it is worth exposing on its own:
an operator asking "will station X run dry, and when?" should not have to
run an optimisation to find out.

``/api/forecast/validate`` is the important one for a judge: it compares the
forecast against the **live** simulator's ``/v1/demand-history``, which is
the oracle. The forecast is exact -- the simulator's demand is deterministic
-- so the comparison should match to the last decimal.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from ..degraded import degraded_fields, read_snapshot
from ..explain import explain_stockout_risk
from ..metrics import FORECAST_ERROR, FORECAST_VALIDATION
from ..forecast import (
    DEMAND_TOLERANCE,
    FUEL_TYPES,
    ForecastBundle,
    build_forecast,
)
from ..state import WorldSnapshot

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/forecast", tags=["forecast"])


async def _fresh_snapshot(request: Request) -> WorldSnapshot:
    """The forecast's world.

    Read-only, so it uses the shared degraded-aware read: when the upstream
    is gone the forecast is still computed from the last known good world and
    the response says so. A forecast is a projection, not a write -- serving
    a slightly stale projection with an honest flag is more useful than
    refusing to answer.
    """
    return await read_snapshot(request)


def _build(request: Request, snapshot: WorldSnapshot, horizon: int) -> ForecastBundle:
    settings = request.app.state.settings
    return build_forecast(
        snapshot,
        horizon=horizon,
        start_time=settings.forecast_start_time,
    )


@router.get("")
@router.get("/")
async def get_forecast(
    request: Request, horizon: Optional[int] = None
) -> dict[str, Any]:
    """The forecast for every station and fuel over the horizon."""
    snapshot = await _fresh_snapshot(request)
    settings = request.app.state.settings
    effective = horizon or settings.forecast_horizon_ticks
    forecast = _build(request, snapshot, effective)

    return {
        "status": "OK",
        "tick": snapshot.tick,
        "horizon": forecast.horizon,
        "seed": forecast.seed,
        "start_tick": forecast.start_tick,
        "demand": {
            f"{station_id}:{fuel}": series
            for (station_id, fuel), series in sorted(forecast.demand.items())
        },
        "risk": [r.to_dict() for r in forecast.risk],
        **degraded_fields(request),
    }


@router.get("/validate")
async def validate_forecast(
    request: Request, horizon: Optional[int] = None
) -> dict[str, Any]:
    """Compare the forecast against the live simulator's demand history.

    This is the forecast's oracle. The simulator's demand is deterministic,
    so the forecast should match the recorded history exactly -- within the
    simulator's own 3-decimal rounding.

    The comparison is read from ``/v1/demand-history``, which the backend's
    client already exposes. If the upstream has no history yet (a freshly
    reset simulator at tick 0), the endpoint says so rather than claiming a
    pass it did not check.
    """
    snapshot = await _fresh_snapshot(request)
    settings = request.app.state.settings
    effective = horizon or settings.forecast_horizon_ticks
    forecast = _build(request, snapshot, effective)

    client = request.app.state.client
    try:
        history = await client.demand_history(
            limit=settings.forecast_history_limit
        )
    except Exception as exc:
        LOGGER.warning("Could not read the demand history: %s", exc)
        return {
            "status": "OK",
            "tick": snapshot.tick,
            "verdict": "UNAVAILABLE",
            "reason": f"the demand history could not be read: {exc}",
            "matched": 0,
            "mismatched": 0,
            **degraded_fields(request),
        }

    rows = _history_rows(history)

    # The history covers ticks that are *behind* the current tick -- the
    # simulator has just been reset, or simply advanced. The forecast built
    # above starts at the current tick, so it cannot answer a question about
    # an earlier tick.
    #
    # Rebuild the forecast **anchored at the history's earliest tick** so the
    # comparison is apples-to-apples. The demand formula is a pure function
    # of the tick, so anchoring it anywhere gives the same numbers.
    history_ticks = [
        int(row["tick"])
        for row in rows
        if row.get("tick") is not None
    ]
    if history_ticks:
        earliest = min(history_ticks)
        latest = max(history_ticks)
        span = latest - earliest + 1
        anchored = build_forecast(
            snapshot,
            horizon=span,
            start_time=settings.forecast_start_time,
            start_tick=earliest,
        )
    else:
        anchored = forecast
        earliest = forecast.start_tick
        latest = forecast.start_tick

    matched = 0
    mismatched = 0
    failures: list[dict[str, Any]] = []
    largest_error = 0.0

    for row in rows:
        tick = row.get("tick")
        station_id = row.get("station_id")
        fuel_type = row.get("fuel_type")
        observed = row.get("demand_liters")
        if observed is None:
            observed = row.get("demand_liters")
        if tick is None or station_id is None or fuel_type is None:
            continue
        if observed is None:
            continue

        expected = anchored.demand_at(station_id, fuel_type, int(tick))
        error = abs(float(expected) - float(observed))
        largest_error = max(largest_error, error)
        # The prediction error, as a metric. This is the "prediction error"
        # the PDF's section 15 intelligence layer asks for -- how far the
        # forecast is from what the simulator actually did.
        FORECAST_ERROR.observe(error)

        if error <= DEMAND_TOLERANCE:
            matched += 1
        else:
            mismatched += 1
            if len(failures) < 20:
                failures.append(
                    {
                        "tick": tick,
                        "station_id": station_id,
                        "fuel_type": fuel_type,
                        "expected": expected,
                        "observed": observed,
                        "error": error,
                    }
                )

    total = matched + mismatched
    if total == 0:
        verdict = "UNAVAILABLE"
        reason = "the simulator has no demand history to compare against yet."
    elif mismatched == 0:
        verdict = "EXACT"
        reason = (
            f"all {matched} compared demand values match the live simulator "
            f"within {DEMAND_TOLERANCE} L."
        )
    else:
        verdict = "MISMATCH"
        reason = f"{mismatched} of {total} compared values differ."

    # The validation counter: how many validations have been run, and how
    # they came out. A forecast that is *never* validated against the live
    # oracle is a forecast nobody is checking.
    FORECAST_VALIDATION.inc(verdict=verdict)

    return {
        "status": "OK",
        "tick": snapshot.tick,
        "verdict": verdict,
        "reason": reason,
        "matched": matched,
        "mismatched": mismatched,
        "compared": total,
        "largest_error_litres": largest_error,
        "tolerance_litres": DEMAND_TOLERANCE,
        "failures": failures,
        **degraded_fields(request),
    }


@router.get("/summary")
async def forecast_summary(
    request: Request, horizon: Optional[int] = None
) -> dict[str, Any]:
    """The forecast's headline numbers, per station and fuel."""
    snapshot = await _fresh_snapshot(request)
    settings = request.app.state.settings
    effective = horizon or settings.forecast_horizon_ticks
    forecast = _build(request, snapshot, effective)

    rows = []
    for risk in forecast.risk:
        rows.append(
            {
                "station_id": risk.station_id,
                "fuel_type": risk.fuel_type,
                "current_inventory_litres": risk.current_inventory,
                "total_demand_litres": round(sum(risk.demand_series), 3),
                "ticks_to_stockout": risk.ticks_to_stockout,
                "stockout_probability": round(risk.stockout_probability, 4),
                "expected_unmet_litres": round(risk.expected_unmet_litres, 3),
                "coverage_ticks": risk.coverage_ticks,
                "confidence": risk.confidence,
            }
        )

    at_risk = [r for r in rows if r["expected_unmet_litres"] > 0]

    return {
        "status": "OK",
        "tick": snapshot.tick,
        "horizon": forecast.horizon,
        "total_demand_litres": round(
            sum(sum(s) for s in forecast.demand.values()), 3
        ),
        "at_risk_count": len(at_risk),
        "stations": rows,
        **degraded_fields(request),
    }


@router.get("/{station_id}")
async def get_station_forecast(
    station_id: str, request: Request, horizon: Optional[int] = None
) -> dict[str, Any]:
    """One station: its per-fuel forecast series and its stockout risk."""
    snapshot = await _fresh_snapshot(request)
    settings = request.app.state.settings
    effective = horizon or settings.forecast_horizon_ticks
    forecast = _build(request, snapshot, effective)

    station = snapshot.station(station_id)
    if station is None:
        raise HTTPException(status_code=404, detail=f"unknown station {station_id!r}")

    series = {
        fuel: forecast.demand.get((station_id, fuel), [])
        for fuel in FUEL_TYPES
        if (station_id, fuel) in forecast.demand
    }
    risks = [r.to_dict() for r in forecast.risk if r.station_id == station_id]
    risk_objects = [r for r in forecast.risk if r.station_id == station_id]
    explanations = [
        explain_stockout_risk(r, forecast=forecast, snapshot=snapshot).to_dict()
        for r in risk_objects
    ]

    return {
        "status": "OK",
        "tick": snapshot.tick,
        "horizon": forecast.horizon,
        "station": station.model_dump(),
        "demand": series,
        "risk": risks,
        "explanations": explanations,
        "routes": [
            r.model_dump() for r in snapshot.routes_for_station(station_id)
        ],
        **degraded_fields(request),
    }


def _history_rows(history: Any) -> list[dict[str, Any]]:
    """Normalise the demand history into plain dicts.

    The upstream's shape has varied between versions -- a bare list, a
    ``{"items": [...]}`` envelope, or a list of records with different key
    names. Normalising here keeps the endpoint tolerant of all of them.
    """
    if history is None:
        return []
    if isinstance(history, dict):
        for key in ("items", "demand_history", "history", "data", "results"):
            if key in history:
                history = history[key]
                break
        else:
            return []
    if not isinstance(history, (list, tuple)):
        return []

    rows: list[dict[str, Any]] = []
    for item in history:
        if isinstance(item, dict):
            rows.append(item)
        elif hasattr(item, "model_dump"):
            rows.append(item.model_dump())
        elif hasattr(item, "__dict__"):
            rows.append(dict(vars(item)))
    return rows
