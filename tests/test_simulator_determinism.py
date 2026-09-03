"""Simulator determinism (spec §9.6, §16.4, acceptance criterion 10).

"A demo that produces different numbers on a re-run in front of judges is a demo
that loses." Asserted in CI, not hoped for.
"""

from __future__ import annotations

import pytest

from services.simulation_service import run_simulation

SCENARIO = {
    "name": "Bus accident — Motorway M2",
    "event_type": "BUS_ACCIDENT",
    "epicenter_lat": 31.6000,
    "epicenter_lon": 74.3000,
    "casualties": 60,
    "severity_mix": {
        "MINOR": 0.35,
        "MODERATE": 0.30,
        "SEVERE": 0.25,
        "CRITICAL": 0.10,
    },
    "onset_profile": "RAMP_6H",
    "duration_hours": 12,
    "seed": 42,
    "iterations": 200,
}


@pytest.fixture(scope="module")
def first_run(session):
    return run_simulation(session, dict(SCENARIO), save=False)


def test_same_seed_produces_identical_totals(session, first_run):
    second = run_simulation(session, dict(SCENARIO), save=False)

    assert second["totals"] == first_run["totals"]


def test_same_seed_produces_identical_requirements(session, first_run):
    second = run_simulation(session, dict(SCENARIO), save=False)

    assert second["requirement_by_group_component"] == (
        first_run["requirement_by_group_component"]
    )
    assert second["donor_mobilization"] == first_run["donor_mobilization"]


def test_different_seed_produces_different_output(session, first_run):
    """Determinism must come from the seed, not from the simulation being
    degenerate."""

    varied = run_simulation(
        session, {**SCENARIO, "seed": 1234}, save=False
    )

    assert varied["totals"] != first_run["totals"]


def test_emergency_transfer_count_respects_its_cap(session, first_run):
    from core import config

    cap = int(config.get("simulation.max_emergency_transfers", 20))

    assert len(first_run["emergency_transfers"]) <= cap, (
        f"simulator returned {len(first_run['emergency_transfers'])} emergency "
        f"transfers against a cap of {cap}"
    )


def test_reported_p95_is_not_below_p50(first_run):
    totals = first_run["totals"]

    assert totals["units_required_p95"] >= totals["units_required_p50"]


def test_monte_carlo_total_is_reported_alongside_any_planning_figure(first_run):
    """The simulator may plan against a conservative sum of per-cell quantiles,
    but the true Monte Carlo distribution has to remain visible — otherwise a
    superadditive sum gets presented as "the P95" and inflates the requirement.
    """

    totals = first_run["totals"]

    assert "monte_carlo_total_p50" in totals
    assert "monte_carlo_total_p95" in totals
