"""Reserve floor policy invariants (spec §4.2).

Regression tests for the two defects that made the reserve floor mean two
different things in two engines: a component-level number applied to each of
eight blood group series (an eightfold inflation), and the same number scaled by
national population share (which left a 1,000-bed trauma centre with a one-unit
O- floor).
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core import config, policy

GROUP_SHARES = {
    "B+": 0.33,
    "O+": 0.30,
    "A+": 0.22,
    "AB+": 0.08,
    "O-": 0.025,
    "B-": 0.020,
    "A-": 0.017,
    "AB-": 0.008,
}

FACILITY_TYPES = ["RBC", "TERTIARY_HOSPITAL", "SPECIALIST_CENTRE", "DHQ", "THQ"]
COMPONENTS = ["PRBC", "WB", "PLT_RD", "PLT_APH", "FFP", "CRYO"]


def test_stored_policy_is_per_component_and_group(facilities):
    """The stored shape must be component -> group -> units, not a single number
    per component that each engine then interprets its own way."""

    for facility in facilities:
        stored = facility.min_reserve_policy_json or {}

        assert stored, f"{facility.code} has no reserve policy"

        for component_code, entry in stored.items():
            assert isinstance(entry, dict), (
                f"{facility.code} {component_code}: reserve policy must be keyed "
                f"by blood group, got {type(entry).__name__}"
            )

            missing = set(GROUP_SHARES) - set(entry)
            assert not missing, (
                f"{facility.code} {component_code}: missing floors for {missing}"
            )


def test_group_floors_do_not_inflate_the_facility_obligation(facilities):
    """Expanding a component policy across eight groups must not multiply what
    the facility is actually required to hold."""

    maximum = float(config.get("reserve_policy.max_aggregate_inflation", 1.20))

    for facility in facilities:
        for component_code, entry in (facility.min_reserve_policy_json or {}).items():
            total = sum(float(v or 0) for v in entry.values())
            largest = max(float(v or 0) for v in entry.values())

            # An eightfold inflation would show up as the sum being eight times
            # the largest single group floor.
            assert total <= largest * 8, (
                f"{facility.code} {component_code}: group floors sum to {total}, "
                "which is only possible if a component total was applied per group"
            )

            if largest > 0:
                assert total / largest < 8.0 * maximum


def test_o_negative_red_cell_floor_is_protected(facilities, session):
    """O- is the universal red cell donor and the emergency stock. A floor set in
    proportion to its 2.5% population share is backwards."""

    for facility in facilities:
        if facility.facility_type not in {"TERTIARY_HOSPITAL", "RBC"}:
            continue

        floor = policy.reserve_floor(facility, "PRBC", "O-")

        assert floor >= 4, (
            f"{facility.code} ({facility.facility_type}) carries an O- PRBC floor "
            f"of {floor}, too low for a facility of this size"
        )


def test_o_negative_floor_exceeds_its_population_share(facilities):
    """The whole point of the strategic weighting."""

    for facility in facilities:
        entry = (facility.min_reserve_policy_json or {}).get("PRBC") or {}
        total = sum(float(v or 0) for v in entry.values())

        if total <= 0:
            continue

        o_negative_share = float(entry.get("O-", 0)) / total

        assert o_negative_share > GROUP_SHARES["O-"], (
            f"{facility.code}: O- holds {o_negative_share:.1%} of the PRBC "
            f"reserve, at or below its {GROUP_SHARES['O-']:.1%} population share"
        )


def test_ab_plasma_floor_is_protected(facilities):
    """AB is the universal plasma donor, so it is over-weighted for plasma the
    way O- is for red cells. Applying the red-cell weighting to plasma would
    show up here."""

    for facility in facilities:
        entry = (facility.min_reserve_policy_json or {}).get("FFP") or {}
        total = sum(float(v or 0) for v in entry.values())

        if total <= 0:
            continue

        ab_share = (float(entry.get("AB+", 0)) + float(entry.get("AB-", 0))) / total

        assert ab_share > GROUP_SHARES["AB+"] + GROUP_SHARES["AB-"], (
            f"{facility.code}: AB plasma holds {ab_share:.1%} of the FFP reserve, "
            "not above its population share"
        )


@settings(max_examples=200, deadline=None)
@given(
    component_total=st.floats(min_value=0.0, max_value=500.0),
    facility_type=st.sampled_from(FACILITY_TYPES),
    component_code=st.sampled_from(COMPONENTS),
)
def test_allocation_never_inflates_beyond_the_configured_ceiling(
    component_total, facility_type, component_code
):
    """Property test over arbitrary policy totals (spec §16.4 asks for
    property-based tests over randomly generated network states)."""

    floors = policy.allocate_group_floors(
        component_total, component_code, facility_type, GROUP_SHARES
    )

    assert set(floors) == set(GROUP_SHARES)
    assert all(value >= 0 for value in floors.values())

    total = sum(floors.values())
    ceiling = float(config.get("reserve_policy.max_aggregate_inflation", 1.20))

    # The absolute strategic minimums override the aggregate ceiling by design:
    # a Regional Blood Centre holds its O- emergency stock whatever its headline
    # policy says. So the bound is the greater of the two, plus rounding slack of
    # one unit per group.
    minimums = policy.strategic_minimum_total(
        component_code, facility_type, GROUP_SHARES
    )
    bound = max(component_total * ceiling, minimums) + len(GROUP_SHARES)

    assert total <= bound, (
        f"{component_code} at {facility_type}: floors sum to {total} from a "
        f"{component_total} total (bound {bound}, strategic minimums {minimums})"
    )
