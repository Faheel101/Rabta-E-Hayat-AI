"""Invariants the forecasting and risk engines must satisfy.

Two defect classes are pinned here, both caught by audit and both invisible in
the output until you go looking:

1. Quantile crossing. P10, P50 and P90 came from different sources — two from a
   discrete count distribution and the middle one from a continuous mean — so
   7,179 stored rows had P50 above P90, and on 196 series the P90 was identically
   zero across all thirty horizon days.

2. A safety band computed by summing per-day quantiles. The quantile of a sum is
   not the sum of quantiles unless the days are perfectly correlated; for an
   intermittent series it collapses to zero, so the P90 buffer asked for nothing
   on exactly the rare-group series where a buffer matters most.

And one display rule: a shelf at or below its reserve floor must never read SAFE.
"""

from __future__ import annotations

import pytest
import numpy as np
from sqlalchemy import func, select, text

from core import demand_dist
from db.models import Forecast, ShortageRisk
from db.session import SessionLocal
from engines.forecast.models import tsb_state


@pytest.fixture(scope="module")
def db():
    session = SessionLocal()

    try:
        yield session
    finally:
        session.close()


# ------------------------------------------------------------ quantile order


def test_no_stored_forecast_has_crossed_quantiles(db):
    """P10 <= P50 <= P90 is not a modelling preference, it is arithmetic. A row
    that violates it is self-contradictory whatever the model said."""

    crossed = db.scalar(
        select(func.count())
        .select_from(Forecast)
        .where((Forecast.p10 > Forecast.p50) | (Forecast.p50 > Forecast.p90))
    )

    assert crossed == 0, f"{crossed:,} forecast rows have crossed quantiles"


def test_no_series_has_an_identically_zero_upper_band(db):
    """A series with real demand whose P90 is zero on every horizon day has no
    safety signal at all — it can never trigger a shortage alert."""

    dead = db.scalar(
        text(
            "SELECT COUNT(*) FROM ("
            "  SELECT facility_id, component_id, blood_group_id FROM forecast "
            "  GROUP BY 1,2,3 HAVING MAX(p90) = 0 AND MAX(p50) > 0)"
        )
    )

    assert dead == 0, f"{dead:,} series have a dead P90 band despite real demand"


def test_forecasts_are_never_negative(db):
    negative = db.scalar(
        select(func.count())
        .select_from(Forecast)
        .where((Forecast.p10 < 0) | (Forecast.p50 < 0) | (Forecast.p90 < 0))
    )

    assert negative == 0


def test_tsb_default_smoothing_is_the_registered_eight_fold_calibration():
    """A fast smoothing pair overreacted to a handful of recent sparse events
    and failed the registered-series baseline gate.  Keep the measured release
    pair explicit so a seemingly harmless default change cannot bypass the
    complete backtest contract."""

    values = np.asarray(([0.0, 0.0, 1.0, 0.0] * 35) + [0.0, 0.0, 5.0])

    assert tsb_state(values) == pytest.approx(
        tsb_state(values, alpha=0.03, beta=0.03)
    )

    calibrated_probability, calibrated_size = tsb_state(values)
    fast_probability, fast_size = tsb_state(values, alpha=0.15, beta=0.15)

    assert calibrated_probability > 0
    assert calibrated_size < fast_size, (
        "the calibrated size estimate must resist one late outlier more than "
        "the retired fast smoother"
    )


# ------------------------------------------------------- the window quantile


def test_the_window_p90_is_never_below_the_window_p50(db):
    crossed = db.scalar(
        select(func.count())
        .select_from(ShortageRisk)
        .where(ShortageRisk.required_p90 < ShortageRisk.required_p50)
    )

    assert crossed == 0


def test_the_p90_requirement_always_exceeds_the_reserve_floor(db):
    """If required_p90 equals the reserve floor, the demand term contributed
    nothing and the safety band is inert."""

    inert = db.scalar(
        select(func.count())
        .select_from(ShortageRisk)
        .where(ShortageRisk.required_p90 <= ShortageRisk.reserve_floor)
    )

    assert inert == 0, f"{inert:,} risk rows have an inert P90 safety band"


def test_a_window_quantile_is_not_a_sum_of_daily_quantiles():
    """The property that makes the old code wrong.

    Summing per-day P90s over a window overstates the window's P90 for
    independent days (variances add, standard deviations do not) and collapses to
    zero for a sparse count series where every daily P90 rounds to zero. Neither
    is the quantile of the total.
    """

    # A sparse series: real demand, but the discrete daily P90 is zero.
    sparse = [(0.0, 0.1, 0.0)] * 8
    mean, sigma = demand_dist.window_moments(sparse)

    summed = sum(row[2] for row in sparse)
    from_moments = mean + demand_dist.quantile_z() * sigma

    assert summed == 0.0, "the sparse fixture no longer demonstrates the collapse"
    assert from_moments > mean > 0, (
        "the window P90 must exceed the window mean even when every daily P90 is zero"
    )

    # A dense series: summing overstates, because sigma grows with sqrt(n).
    dense = [(2.0, 5.0, 8.0)] * 9
    mean, sigma = demand_dist.window_moments(dense)

    summed = sum(row[2] for row in dense)
    from_moments = mean + demand_dist.quantile_z() * sigma

    assert from_moments < summed, (
        "summing daily P90s should overstate the window P90 for independent days"
    )
    assert from_moments > mean


# ------------------------------------------------------------- the bucketing


def test_an_empty_shelf_is_never_painted_safe(db):
    """The failure mode that matters on a dashboard: green means 'do nothing',
    and doing nothing about an empty shelf is how a patient goes without."""

    lying = db.scalar(
        select(func.count())
        .select_from(ShortageRisk)
        .where(
            ShortageRisk.risk_bucket == "SAFE",
            ShortageRisk.projected_available <= ShortageRisk.reserve_floor,
        )
    )

    assert lying == 0, f"{lying:,} series read SAFE while at or below their reserve floor"


def test_stock_position_can_only_raise_severity_never_lower_it():
    thresholds = (0.05, 0.15, 0.35)

    # A high probability must stay CRITICAL even with plenty on the shelf.
    assert (
        demand_dist.bucket(0.90, thresholds, available=500, reserve_floor=20)
        == "CRITICAL"
    )

    # A low probability with healthy stock is genuinely SAFE.
    assert (
        demand_dist.bucket(0.01, thresholds, available=500, reserve_floor=20) == "SAFE"
    )

    # The same low probability at the floor is not.
    assert (
        demand_dist.bucket(0.01, thresholds, available=20, reserve_floor=20)
        == "WARNING"
    )


def test_below_the_strategic_minimum_is_critical():
    thresholds = (0.05, 0.15, 0.35)

    assert (
        demand_dist.bucket(
            0.0, thresholds, available=0, reserve_floor=10, strategic_minimum=2
        )
        == "CRITICAL"
    )


def test_callers_without_stock_information_still_work():
    """The stock arguments are optional, so an existing caller that knows only
    the probability keeps its previous behaviour rather than crashing."""

    thresholds = (0.05, 0.15, 0.35)

    assert demand_dist.bucket(0.01, thresholds) == "SAFE"
    assert demand_dist.bucket(0.90, thresholds) == "CRITICAL"
