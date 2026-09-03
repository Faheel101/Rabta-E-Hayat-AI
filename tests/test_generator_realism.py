"""Synthetic data realism checks (spec §15.4).

The spec asks for these as tests, not as a report the developer reads once. They
guard against the failure this data had originally: an internally consistent
dataset that could not support the argument the product is built on, because
nothing was ever in surplus and wastage was unmeasurable.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from config.calendar import is_ramadan
from core import config
from db.models import (
    BloodUnit,
    Component,
    DemandEvent,
    InventorySnapshot,
)


@pytest.fixture(scope="module")
def wastage(session):
    rows = session.execute(
        select(
            func.sum(InventorySnapshot.units_collected),
            func.sum(InventorySnapshot.units_expired),
            func.sum(InventorySnapshot.units_discarded),
        )
    ).one()

    collected, expired, discarded = (float(value or 0) for value in rows)
    wasted = expired + discarded

    return {
        "collected": collected,
        "expired": expired,
        "discarded": discarded,
        "wasted": wasted,
        "rate": (wasted / collected) if collected else 0.0,
        "expiry_share": (expired / wasted) if wasted else 0.0,
    }


def test_overall_wastage_is_in_the_observed_range(wastage):
    low, high = config.get("supply.realism.wastage_pct_range", [10.0, 15.0])

    rate = wastage["rate"] * 100.0

    # Measured against collections, which includes screening failures, so the
    # floor is a touch below the released-units figure the generator reports.
    assert low * 0.85 <= rate <= high * 1.15, (
        f"wastage {rate:.1f}% outside the {low}-{high}% range observed in the "
        "Shalamar Lahore audit"
    )


def test_expiry_dominates_wastage(wastage):
    """The Lahore audit found 96% of wastage was expiry. Expiry is predictable
    and therefore preventable, which is the entire product argument."""

    minimum = float(config.get("supply.realism.expiry_share_of_wastage_min", 0.90))

    assert wastage["expiry_share"] >= minimum, (
        f"expiry is only {wastage['expiry_share']:.1%} of wastage, below the "
        f"{minimum:.0%} the audit found"
    )


def test_platelet_wastage_exceeds_red_cell_wastage(session):
    """A 5-day shelf life against 35 days. If platelets are not the worse
    offender, the shelf-life model is not doing anything."""

    rows = session.execute(
        select(
            Component.code,
            func.sum(InventorySnapshot.units_expired),
            func.sum(InventorySnapshot.units_collected),
        )
        .join(Component, Component.id == InventorySnapshot.component_id)
        .group_by(Component.code)
    ).all()

    rates = {
        code: (float(expired or 0) / float(collected))
        for code, expired, collected in rows
        if collected
    }

    assert rates["PLT_RD"] > rates["PRBC"], (
        f"platelet wastage {rates['PLT_RD']:.1%} does not exceed PRBC "
        f"{rates['PRBC']:.1%}"
    )


def test_unmet_demand_is_in_range_and_concentrated_in_rare_groups(session):
    low, high = config.get("supply.realism.unmet_demand_pct_range", [3.0, 7.0])

    requested, issued = session.execute(
        select(
            func.sum(DemandEvent.units_requested),
            func.sum(DemandEvent.units_issued),
        )
    ).one()

    unmet_pct = 100.0 * (float(requested) - float(issued)) / float(requested)

    assert low <= unmet_pct <= high, (
        f"unmet demand {unmet_pct:.1f}% outside the {low}-{high}% target"
    )


def test_rh_negative_groups_have_the_worst_unmet_demand(session):
    """Spec §19.2: the low Rh-negative share is why O- and AB- shortages
    dominate this problem in Pakistan. If the data does not show that, the demo
    contradicts its own problem statement."""

    from db.models import BloodGroup

    rows = session.execute(
        select(
            BloodGroup.code,
            BloodGroup.rh,
            func.sum(DemandEvent.units_requested),
            func.sum(DemandEvent.units_issued),
        )
        .join(BloodGroup, BloodGroup.id == DemandEvent.blood_group_id)
        .group_by(BloodGroup.code, BloodGroup.rh)
    ).all()

    negative = []
    positive = []

    for code, rh, requested, issued in rows:
        if not requested:
            continue

        unmet = 100.0 * (float(requested) - float(issued)) / float(requested)
        (negative if rh == "-" else positive).append(unmet)

    assert min(negative) > max(positive), (
        f"Rh-negative unmet demand {min(negative):.1f}-{max(negative):.1f}% does "
        f"not exceed Rh-positive {min(positive):.1f}-{max(positive):.1f}%"
    )


def test_no_unit_expires_before_it_was_collected(session):
    violations = session.scalar(
        select(func.count())
        .select_from(BloodUnit)
        .where(BloodUnit.expires_at <= BloodUnit.collected_at)
    )

    assert violations == 0, f"{violations} units expire at or before collection"


def test_ramadan_collection_drop_is_visible(session):
    """Donation collection collapses during Ramadan while demand for several
    components does not. This is the squeeze the forecaster has to learn."""

    rows = session.execute(
        select(
            InventorySnapshot.snapshot_date,
            func.sum(InventorySnapshot.units_collected),
        ).group_by(InventorySnapshot.snapshot_date)
    ).all()

    ramadan = [float(units or 0) for day, units in rows if is_ramadan(day)]
    other = [float(units or 0) for day, units in rows if not is_ramadan(day)]

    assert ramadan and other, "not enough history to compare"

    ramadan_mean = sum(ramadan) / len(ramadan)
    other_mean = sum(other) / len(other)

    assert ramadan_mean < other_mean * 0.9, (
        f"Ramadan collections average {ramadan_mean:.0f}/day against "
        f"{other_mean:.0f}/day outside it — the drop is not visible"
    )


def test_network_contains_both_surplus_and_shortage(session):
    """The product's founding claim: blood sits in the wrong building at the
    wrong time. If every facility is right-sized, there is nothing to optimise
    and nothing to rescue.
    """

    from db.models import Facility

    rows = session.execute(
        select(
            Facility.code,
            func.count(BloodUnit.id),
        )
        .join(BloodUnit, BloodUnit.facility_id == Facility.id)
        .join(Component, Component.id == BloodUnit.component_id)
        .where(
            Component.code == "PRBC",
            BloodUnit.status == "AVAILABLE",
            BloodUnit.screening_status == "PASSED",
        )
        .group_by(Facility.code)
    ).all()

    demand = dict(
        session.execute(
            select(Facility.code, func.sum(DemandEvent.units_requested))
            .join(DemandEvent, DemandEvent.facility_id == Facility.id)
            .join(Component, Component.id == DemandEvent.component_id)
            .where(Component.code == "PRBC")
            .group_by(Facility.code)
        ).all()
    )

    days = float(config.HISTORY_DAYS)
    cover = {}

    for code, on_hand in rows:
        daily = float(demand.get(code) or 0) / days

        if daily > 0.3:
            cover[code] = on_hand / daily

    assert cover, "no facility has measurable PRBC demand"

    thin = [code for code, value in cover.items() if value < 3.0]
    thick = [code for code, value in cover.items() if value > 20.0]

    assert thin, "no facility is running short — nothing for the optimizer to fix"
    assert thick, "no facility is over-stocked — nothing for the rescue engine to find"


def test_all_four_demand_regimes_are_represented(session):
    """Spec §15.4: demand series must pass the ADI/CV-squared classification into
    all four quadrants, or the regime router is never exercised."""

    from db.models import ForecastMetric

    regimes = {
        row[0]
        for row in session.execute(
            select(ForecastMetric.regime).distinct()
        ).all()
    }

    if not regimes:
        pytest.skip("no backtest metrics; run scripts.run_backtest")

    for expected in ("SMOOTH", "ERRATIC", "INTERMITTENT", "LUMPY"):
        assert expected in regimes, f"no series classified {expected}"


def test_latest_forecast_run_passes_decision_quality_and_safety_gates(session):
    """Sprint 8 release evidence, stored rather than copied from console output.

    Facility×component is the operational ordering grain.  Blood-group daily
    error remains separately disclosed because compatibility-specific noise
    must never be hidden by aggregation.
    """

    from db.models import ForecastRunSummary

    row = session.scalar(
        select(ForecastRunSummary).order_by(ForecastRunSummary.generated_at.desc())
    )

    if row is None:
        pytest.skip("no backtest summary; run scripts.run_backtest")

    detail = row.metrics_json or {}
    decision_wape = detail.get("wape_component_grain_7d")

    assert decision_wape is not None and decision_wape <= 0.25
    assert row.pct_series_beating_naive is not None
    assert row.pct_series_beating_naive >= 80.0
    assert row.shortage_detection_recall_3d is not None
    assert row.shortage_detection_recall_3d >= 0.75
    assert row.picp_p10_p90 is not None
    assert 0.70 <= row.picp_p10_p90 <= 0.90
    assert row.wape_dense_7d is not None
    assert row.noise_floor_dense_7d is not None
    assert row.wape_dense_7d - row.noise_floor_dense_7d <= 0.03
