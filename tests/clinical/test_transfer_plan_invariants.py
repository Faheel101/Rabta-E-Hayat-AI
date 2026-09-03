"""Transfer plan invariants (spec §16.4).

"No transfer plan may ever violate ABO/Rh compatibility, shelf-life feasibility,
cold-chain limits, or a facility's reserve floor."

These run against the plan actually stored in the database, not a constructed
one. A plan that satisfies these on synthetic fixtures while the shipped plan
violates them is worth nothing.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select

from config.settings import DEMO_DATE
from core import config, geo, policy
from db.models import (
    BloodGroup,
    BloodUnit,
    Compatibility,
    Component,
    Facility,
    Transfer,
    TransferPlan,
)

DEMO_DATETIME = datetime.combine(DEMO_DATE, time(8, 0, 0), tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def transfers(session):
    current = session.scalars(
        select(TransferPlan)
        .where(TransferPlan.status == "GENERATED")
        .order_by(TransferPlan.created_at.desc())
    ).first()
    rows = (
        session.scalars(select(Transfer).where(Transfer.plan_id == current.id)).all()
        if current is not None
        else []
    )

    if not rows:
        pytest.skip("No transfer plan in the database; run scripts.run_optimizer")

    return rows


@pytest.fixture(scope="module")
def lookups(session):
    facilities = session.scalars(select(Facility)).all()
    components = session.scalars(select(Component)).all()
    groups = session.scalars(select(BloodGroup)).all()

    compatible = {
        (component_id, recipient_id, donor_id): (rank, bool(override))
        for component_id, recipient_id, donor_id, rank, override in session.execute(
            select(
                Compatibility.component_id,
                Compatibility.recipient_group_id,
                Compatibility.donor_group_id,
                Compatibility.preference_rank,
                Compatibility.requires_override,
            ).where(Compatibility.is_compatible.is_(True))
        ).all()
    }

    return {
        "facility": {f.id: f for f in facilities},
        "component": {c.id: c for c in components},
        "group": {g.id: g for g in groups},
        "compatible": compatible,
        "travel": geo.build_travel_matrix(facilities),
    }


def test_plan_exists_and_is_singular(session):
    plans = session.scalars(
        select(TransferPlan).where(TransferPlan.status == "GENERATED")
    ).all()
    assert len(plans) == 1, f"expected exactly one active plan, found {len(plans)}"


def test_every_transfer_is_abo_rh_compatible(transfers, lookups):
    """The clinical spine. A violation here is a patient safety event."""

    for transfer in transfers:
        recipient_id = transfer.recipient_group_id

        if recipient_id is None:
            pytest.fail(
                f"transfer {transfer.id} records no recipient group, so its "
                "compatibility path cannot be audited"
            )

        key = (transfer.component_id, recipient_id, transfer.blood_group_id)
        entry = lookups["compatible"].get(key)

        component = lookups["component"][transfer.component_id].code
        donor = lookups["group"][transfer.blood_group_id].code
        recipient = lookups["group"][recipient_id].code

        assert entry is not None, (
            f"transfer {transfer.id} moves {component} {donor} to a {recipient} "
            "recipient, which is not a compatible pair"
        )


def test_no_routine_transfer_requires_a_clinical_override(transfers, lookups):
    """Spec §19.1: ABO-incompatible platelets need an explicit override, so they
    must not appear in a routine plan generated without one."""

    if config.get("optimizer.allow_override_compatibility", False):
        pytest.skip("plan was generated with overrides explicitly enabled")

    for transfer in transfers:
        entry = lookups["compatible"].get(
            (transfer.component_id, transfer.recipient_group_id, transfer.blood_group_id)
        )

        if entry is None:
            continue

        _, requires_override = entry

        assert not requires_override, (
            f"transfer {transfer.id} uses a pair that requires a clinical "
            "override, in a routine plan"
        )


def test_no_transfer_exceeds_its_cold_chain_transport_limit(transfers, lookups):
    """Constraint 5. Platelets have a 6-hour limit; red cells 24 in a validated
    box. Arriving out of temperature range destroys the unit."""

    for transfer in transfers:
        component = lookups["component"][transfer.component_id]
        limit_minutes = float(component.max_transport_hours or 24.0) * 60.0

        actual = lookups["travel"].get(
            (transfer.from_facility_id, transfer.to_facility_id)
        )

        assert actual is not None, f"transfer {transfer.id} has no known route"
        assert actual <= limit_minutes, (
            f"transfer {transfer.id} moves {component.code} over {actual} min, "
            f"exceeding its {limit_minutes:.0f} min transport limit"
        )


def test_every_selected_unit_survives_the_journey(session, transfers, lookups):
    """Constraint 4: remaining shelf life must cover travel plus the handling
    buffer. A unit that arrives expired is worse than one that expires in place."""

    buffer_hours = float(config.get("expiry.handling_buffer_hours", 12))

    unit_ids = [uid for transfer in transfers for uid in (transfer.unit_ids or [])]

    assert unit_ids, "no plan names any physical units; FEFO selection did not run"

    expiry_by_unit = {
        row[0]: row[1]
        for row in session.execute(
            select(BloodUnit.id, BloodUnit.expires_at).where(
                BloodUnit.id.in_(unit_ids)
            )
        ).all()
    }

    for transfer in transfers:
        travel_minutes = lookups["travel"][
            (transfer.from_facility_id, transfer.to_facility_id)
        ]
        required = timedelta(hours=travel_minutes / 60.0 + buffer_hours)

        for unit_id in transfer.unit_ids or []:
            expires_at = expiry_by_unit.get(unit_id)
            assert expires_at is not None, f"unit {unit_id} not found"

            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)

            assert expires_at - DEMO_DATETIME >= required, (
                f"transfer {transfer.id} selects unit {unit_id}, which expires "
                f"at {expires_at} — before it could arrive and be used"
            )


def test_no_facility_is_drained_below_its_reserve_floor(session, transfers, lookups):
    """Constraint 1, and the constraint that makes this tool politically
    acceptable to a hospital administrator."""

    on_hand: dict = {}

    rows = session.execute(
        select(
            BloodUnit.facility_id,
            BloodUnit.component_id,
            BloodUnit.blood_group_id,
        ).where(
            BloodUnit.status == "AVAILABLE",
            BloodUnit.screening_status == "PASSED",
            BloodUnit.expires_at > DEMO_DATETIME,
        )
    ).all()

    for facility_id, component_id, group_id in rows:
        key = (facility_id, component_id, group_id)
        on_hand[key] = on_hand.get(key, 0) + 1

    outflow: dict = {}

    for transfer in transfers:
        key = (
            transfer.from_facility_id,
            transfer.component_id,
            transfer.blood_group_id,
        )
        outflow[key] = outflow.get(key, 0) + transfer.units

    for key, units_out in outflow.items():
        facility_id, component_id, group_id = key

        facility = lookups["facility"][facility_id]
        component_code = lookups["component"][component_id].code
        group_code = lookups["group"][group_id].code

        floor = policy.reserve_floor(facility, component_code, group_code)
        remaining = on_hand.get(key, 0) - units_out

        assert remaining >= floor, (
            f"{facility.name_en} would hold {remaining} units of "
            f"{component_code} {group_code} after this plan, below its reserve "
            f"floor of {floor:.0f}"
        )


def test_no_physical_unit_is_assigned_to_two_transfers(transfers):
    """A plan that promises the same bag to two hospitals is not executable."""

    seen = set()

    for transfer in transfers:
        for unit_id in transfer.unit_ids or []:
            assert unit_id not in seen, (
                f"unit {unit_id} is assigned to more than one transfer"
            )
            seen.add(unit_id)


def test_unit_count_matches_declared_units(transfers):
    for transfer in transfers:
        assert len(transfer.unit_ids or []) == transfer.units, (
            f"transfer {transfer.id} declares {transfer.units} units but names "
            f"{len(transfer.unit_ids or [])}"
        )


def test_units_are_selected_first_expiry_first_out(session, transfers, lookups):
    """Never send the freshest bag.

    Compared against units left behind by the whole plan, not by the transfer
    under examination. Several transfers can draw on the same source series for
    different destinations, and a unit shipped by a sibling transfer has not been
    "kept" — an earlier version of this test treated it as kept and reported a
    FEFO violation where the plan had correctly sent the oldest unit out on a
    shorter route.
    """

    claimed = {
        unit_id for transfer in transfers for unit_id in (transfer.unit_ids or [])
    }

    for transfer in transfers:
        selected_ids = set(transfer.unit_ids or [])

        if not selected_ids:
            continue

        rows = session.execute(
            select(BloodUnit.id, BloodUnit.expires_at).where(
                BloodUnit.facility_id == transfer.from_facility_id,
                BloodUnit.component_id == transfer.component_id,
                BloodUnit.blood_group_id == transfer.blood_group_id,
                BloodUnit.status == "AVAILABLE",
                BloodUnit.screening_status == "PASSED",
                BloodUnit.cold_chain_breach_count == 0,
                BloodUnit.expires_at > DEMO_DATETIME,
            )
        ).all()

        travel_minutes = lookups["travel"][
            (transfer.from_facility_id, transfer.to_facility_id)
        ]
        buffer_hours = float(config.get("expiry.handling_buffer_hours", 12))
        cutoff = DEMO_DATETIME + timedelta(
            hours=travel_minutes / 60.0 + buffer_hours
        )

        sent = []
        kept = []

        for unit_id, expires_at in rows:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)

            if unit_id in selected_ids:
                sent.append(expires_at)
            elif unit_id not in claimed and expires_at > cutoff:
                # Only units left behind by the entire plan, and eligible for
                # this journey, are comparable.
                kept.append(expires_at)

        if not sent or not kept:
            continue

        assert max(sent) <= min(kept), (
            f"transfer {transfer.id} sent a unit expiring {max(sent)} while "
            f"keeping an eligible unit expiring {min(kept)} — not FEFO"
        )


def test_plan_impact_matches_the_plan_as_stored(session, transfers):
    """Regression test for the original defect: the plan claimed 343 rescued plus
    261 shortage-averted units while persisting 284 units in total."""

    plan = session.scalars(
        select(TransferPlan).where(TransferPlan.status == "GENERATED")
    ).first()
    stored = (plan.parameters_json or {}).get("plan_as_persisted") or {}

    assert stored, "plan does not record what was actually persisted"

    assert stored["transfers"] == len(transfers)
    assert stored["units"] == sum(transfer.units for transfer in transfers)
    assert stored["shipments"] == len(
        {(t.from_facility_id, t.to_facility_id) for t in transfers}
    )


def test_shipment_count_respects_the_consolidation_limit(session, transfers):
    """Constraint 8. A plan of forty separate courier runs is not executable."""

    maximum = int(config.get("optimizer.max_shipments_per_run", 12))
    routes = {(t.from_facility_id, t.to_facility_id) for t in transfers}

    assert len(routes) <= maximum, (
        f"plan uses {len(routes)} shipments, above the limit of {maximum}"
    )


def test_no_facility_both_sends_and_receives_the_same_series(transfers):
    """Constraint 9. Moving a group out of a facility and back into it in the
    same plan is a sign the deficit and surplus definitions disagree."""

    sends = {
        (t.from_facility_id, t.component_id, t.blood_group_id) for t in transfers
    }
    receives = {
        (t.to_facility_id, t.component_id, t.blood_group_id) for t in transfers
    }

    overlap = sends & receives

    assert not overlap, f"{len(overlap)} series both sent and received: {list(overlap)[:3]}"
