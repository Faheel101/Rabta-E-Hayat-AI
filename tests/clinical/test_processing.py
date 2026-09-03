"""Component processing: what a bag actually yields, and what it loses.

Two properties carry the weight.

The separation window is enforced PER COMPONENT. A bag spun twelve hours after
collection is not a wasted bag — it yields red cells, but the platelet is gone.
Treating the window as all-or-nothing would either throw away usable red cells or
issue a platelet that has been sitting at 4°C, and both are wrong.

And yield is not always complete. The generator recorded units_expected equal to
units_produced on all 68,215 separations, which asserts a processing loss rate of
zero. No blood bank has that, and a report that can only print 100% is one people
stop reading.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.clock import DEMO_DATETIME
from db.models import (
    AuditLog,
    BloodUnit,
    Component,
    ComponentProduction,
    Donation,
    Donor,
    Facility,
)
from services import lab
from services import processing
from services import screening as screening_service
from services import sessions as session_service
from services.audit import Actor, PermissionDenied, ServiceError

HEALTHY = {
    "haemoglobin_g_dl": 15.0,
    "weight_kg": 72.0,
    "systolic_bp": 120,
    "diastolic_bp": 78,
    "pulse_bpm": 72,
    "temperature_c": 36.7,
}

ALL_CLEAR = {q["key"]: False for q in screening_service.eligibility.QUESTIONS}


@pytest.fixture
def db(scratch_database):
    session = Session(bind=scratch_database)

    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def facility(db):
    return db.scalars(
        select(Facility).where(Facility.is_active.is_(True)).limit(1)
    ).first()


def _actor(facility, role, name, user_id):
    return Actor(
        user_id=user_id,
        display_name=name,
        role=role,
        facility_id=facility.id,
        organization_id=facility.organization_id,
    )


@pytest.fixture
def officer(facility):
    return _actor(facility, "BLOOD_BANK_OFFICER", "Proc Officer", "proc-officer")


@pytest.fixture
def technologist(facility):
    return _actor(facility, "LAB_TECHNOLOGIST", "Proc Tech A", "proc-tech-a")


@pytest.fixture
def verifier(facility):
    return _actor(facility, "LAB_TECHNOLOGIST", "Proc Tech B", "proc-tech-b")


@pytest.fixture
def phlebotomist(facility):
    return _actor(facility, "PHLEBOTOMIST", "Proc Phleb", "proc-phleb")


def _released_donation(
    db, officer, phlebotomist, technologist, verifier, facility, *, bag_type="TRIPLE"
) -> Donation:
    """A donation collected, tested clean and released — ready to separate."""

    session_row = session_service.open_session(db, officer, session_type="IN_HOUSE")

    donor = screening_service.register_donor(
        db,
        officer,
        full_name="Processing Flow Donor",
        gender="MALE",
        date_of_birth=DEMO_DATETIME.date().replace(year=DEMO_DATETIME.year - 30),
        blood_group_id=2,
    )

    draft = screening_service.start_screening(
        db, phlebotomist, donor_id=donor.id, session_id=session_row.id
    )
    screening_service.save_draft(
        db, phlebotomist, screening_id=draft.id, vitals=HEALTHY, answers=ALL_CLEAR
    )
    screening_service.finalise_screening(db, phlebotomist, screening_id=draft.id)

    donation = screening_service.record_donation(
        db, phlebotomist, screening_id=draft.id, bag_type=bag_type
    )

    for marker in lab.required_tests():
        run = lab.open_run(db, technologist, test_code=marker, kit_lot=f"L-{marker}")
        lab.record_results(
            db, technologist, run_id=run.id, results={donation.id: "NON_REACTIVE"}
        )

    lab.release(db, verifier, donation_id=donation.id)
    db.refresh(donation)

    return donation


# ------------------------------------------------------------------ windows


def test_the_window_is_per_component_not_per_bag():
    """A bag spun late loses its platelet, not its red cells."""

    late = DEMO_DATETIME - timedelta(hours=12)

    assert processing.window_status("PLT_RD", late)["allowed"] is False
    assert processing.window_status("FFP", late)["allowed"] is False
    assert processing.window_status("PRBC", late)["allowed"] is True


def test_a_fresh_bag_can_yield_everything():
    fresh = DEMO_DATETIME - timedelta(hours=2)

    for code in ("PRBC", "PLT_RD", "FFP"):
        assert processing.window_status(code, fresh)["allowed"] is True


def test_the_window_reports_how_long_is_left():
    """A technologist deciding what to spin next needs the number, not a
    yes/no."""

    status = processing.window_status("PLT_RD", DEMO_DATETIME - timedelta(hours=6))

    assert status["hours_remaining"] == pytest.approx(2.0, abs=0.1)


# --------------------------------------------------------------- separation


def test_separating_creates_the_units(
    db, officer, phlebotomist, technologist, verifier, facility
):
    """Units are created HERE, not at collection. A bag exists from the moment
    it is drawn; its components exist when somebody spins it."""

    donation = _released_donation(
        db, officer, phlebotomist, technologist, verifier, facility
    )

    record = processing.separate(db, technologist, donation_id=donation.id)

    units = db.scalars(
        select(BloodUnit).where(BloodUnit.donation_id == donation.id)
    ).all()

    issuable = {
        db.get(Component, unit.component_id).code
        for unit in units
        if unit.status == "AVAILABLE"
    }

    assert issuable == {"PRBC", "PLT_RD", "FFP"}
    assert record.units_produced == 3
    assert record.units_expected == 3


def test_the_parent_bag_is_consumed_so_blood_is_not_counted_twice(
    db, officer, phlebotomist, technologist, verifier, facility
):
    """A bag that has been spun no longer exists as whole blood. Leaving it
    available alongside its own components would count the same blood twice —
    once as a bag and again as the products made from it."""

    donation = _released_donation(
        db, officer, phlebotomist, technologist, verifier, facility
    )
    processing.separate(db, technologist, donation_id=donation.id)

    parent = db.scalars(
        select(BloodUnit).where(
            BloodUnit.donation_id == donation.id, BloodUnit.din == donation.din
        )
    ).first()

    assert parent is not None
    assert parent.status == "SEPARATED"
    assert parent.discard_reason == "SEPARATED_INTO_COMPONENTS"

    available = db.scalars(
        select(BloodUnit).where(
            BloodUnit.donation_id == donation.id, BloodUnit.status == "AVAILABLE"
        )
    ).all()

    assert len(available) == 3, "the parent bag is still counted as stock"


def test_a_bag_already_issued_as_whole_blood_cannot_be_separated(
    db, officer, phlebotomist, technologist, verifier, facility
):
    """Released whole blood is issuable AS whole blood. Once somebody has
    committed it to a patient it cannot also be spun into components."""

    donation = _released_donation(
        db, officer, phlebotomist, technologist, verifier, facility
    )

    parent = db.scalars(
        select(BloodUnit).where(
            BloodUnit.donation_id == donation.id, BloodUnit.din == donation.din
        )
    ).first()
    parent.status = "RESERVED"
    db.commit()

    with pytest.raises(ServiceError) as raised:
        processing.separate(db, technologist, donation_id=donation.id)

    assert raised.value.code == "BAG_COMMITTED"


def test_units_expire_from_collection_not_from_separation(
    db, officer, phlebotomist, technologist, verifier, facility
):
    """Dating shelf life from the spin would silently extend every unit's life
    by however long processing took."""

    donation = _released_donation(
        db, officer, phlebotomist, technologist, verifier, facility
    )
    processing.separate(db, technologist, donation_id=donation.id)

    for unit in db.scalars(
        select(BloodUnit).where(BloodUnit.donation_id == donation.id)
    ).all():
        component = db.get(Component, unit.component_id)
        expected = donation.collected_at + timedelta(
            days=int(component.shelf_life_days)
        )

        assert unit.expires_at == expected
        assert unit.collected_at == donation.collected_at


def test_a_bag_cannot_be_separated_twice(
    db, officer, phlebotomist, technologist, verifier, facility
):
    donation = _released_donation(
        db, officer, phlebotomist, technologist, verifier, facility
    )
    processing.separate(db, technologist, donation_id=donation.id)

    with pytest.raises(ServiceError) as raised:
        processing.separate(db, technologist, donation_id=donation.id)

    assert raised.value.code == "ALREADY_SEPARATED"


def test_an_untested_bag_is_separated_into_quarantined_components(
    db, officer, phlebotomist, technologist, facility
):
    """Separation comes BEFORE the lab finishes, not after.

    The panel takes longer than the eight-hour platelet window, so a bank that
    waited for results before spinning would lose every platelet it collected.
    The components are created quarantined and become issuable when the lab
    releases the donation.
    """

    session_row = session_service.open_session(db, officer, session_type="IN_HOUSE")
    donor = screening_service.register_donor(
        db,
        officer,
        full_name="Untested Bag Donor",
        gender="MALE",
        date_of_birth=DEMO_DATETIME.date().replace(year=DEMO_DATETIME.year - 30),
        blood_group_id=2,
    )
    draft = screening_service.start_screening(
        db, phlebotomist, donor_id=donor.id, session_id=session_row.id
    )
    screening_service.save_draft(
        db, phlebotomist, screening_id=draft.id, vitals=HEALTHY, answers=ALL_CLEAR
    )
    screening_service.finalise_screening(db, phlebotomist, screening_id=draft.id)
    donation = screening_service.record_donation(db, phlebotomist, screening_id=draft.id)

    processing.separate(db, technologist, donation_id=donation.id)

    components = db.scalars(
        select(BloodUnit).where(
            BloodUnit.donation_id == donation.id, BloodUnit.din != donation.din
        )
    ).all()

    assert components, "the bag was not separated"

    for unit in components:
        assert unit.status == "QUARANTINE", (
            "a component from an untested bag is issuable"
        )
        assert unit.screening_status == "PENDING"


def test_release_makes_the_separated_components_available(
    db, officer, phlebotomist, technologist, verifier, facility
):
    """The lab clearing the donation is what turns its components into stock."""

    session_row = session_service.open_session(db, officer, session_type="IN_HOUSE")
    donor = screening_service.register_donor(
        db,
        officer,
        full_name="Separate Then Release Donor",
        gender="MALE",
        date_of_birth=DEMO_DATETIME.date().replace(year=DEMO_DATETIME.year - 30),
        blood_group_id=2,
    )
    draft = screening_service.start_screening(
        db, phlebotomist, donor_id=donor.id, session_id=session_row.id
    )
    screening_service.save_draft(
        db, phlebotomist, screening_id=draft.id, vitals=HEALTHY, answers=ALL_CLEAR
    )
    screening_service.finalise_screening(db, phlebotomist, screening_id=draft.id)
    donation = screening_service.record_donation(db, phlebotomist, screening_id=draft.id)

    # Spin first — the platelet cannot wait for the panel.
    processing.separate(db, technologist, donation_id=donation.id)

    for marker in lab.required_tests():
        run = lab.open_run(db, technologist, test_code=marker, kit_lot=f"L2-{marker}")
        lab.record_results(
            db, technologist, run_id=run.id, results={donation.id: "NON_REACTIVE"}
        )

    lab.release(db, verifier, donation_id=donation.id)

    components = db.scalars(
        select(BloodUnit).where(
            BloodUnit.donation_id == donation.id, BloodUnit.din != donation.din
        )
    ).all()

    for unit in components:
        assert unit.status == "AVAILABLE", (
            "release did not make the separated components issuable"
        )
        assert unit.screening_status == "PASSED"


def test_a_quarantined_bag_cannot_be_separated(
    db, officer, phlebotomist, technologist, facility
):
    session_row = session_service.open_session(db, officer, session_type="IN_HOUSE")
    donor = screening_service.register_donor(
        db,
        officer,
        full_name="Reactive Bag Donor",
        gender="MALE",
        date_of_birth=DEMO_DATETIME.date().replace(year=DEMO_DATETIME.year - 30),
        blood_group_id=2,
    )
    draft = screening_service.start_screening(
        db, phlebotomist, donor_id=donor.id, session_id=session_row.id
    )
    screening_service.save_draft(
        db, phlebotomist, screening_id=draft.id, vitals=HEALTHY, answers=ALL_CLEAR
    )
    screening_service.finalise_screening(db, phlebotomist, screening_id=draft.id)
    donation = screening_service.record_donation(db, phlebotomist, screening_id=draft.id)

    run = lab.open_run(db, technologist, test_code="HCV", kit_lot="L-R")
    lab.record_results(db, technologist, run_id=run.id, results={donation.id: "REACTIVE"})

    with pytest.raises(ServiceError) as raised:
        processing.separate(db, technologist, donation_id=donation.id)

    assert raised.value.code == "QUARANTINED"


# ---------------------------------------------------------------- yield loss


def test_a_component_not_produced_needs_a_reason(
    db, officer, phlebotomist, technologist, verifier, facility
):
    """An unattributed loss is one nobody can reduce."""

    donation = _released_donation(
        db, officer, phlebotomist, technologist, verifier, facility
    )

    with pytest.raises(ServiceError) as raised:
        processing.separate(
            db, technologist, donation_id=donation.id, produce=["PRBC", "FFP"]
        )

    assert raised.value.code == "LOSS_REASON_REQUIRED"


def test_a_recorded_loss_is_attributed(
    db, officer, phlebotomist, technologist, verifier, facility
):
    donation = _released_donation(
        db, officer, phlebotomist, technologist, verifier, facility
    )

    record = processing.separate(
        db,
        technologist,
        donation_id=donation.id,
        produce=["PRBC", "FFP"],
        losses={"PLT_RD": "FAILED_SPIN"},
    )

    assert record.units_expected == 3
    assert record.units_produced == 2
    assert record.loss_reasons["PLT_RD"] == "FAILED_SPIN"
    assert set(record.produced_components) == {"PRBC", "FFP"}

    codes = {
        db.get(Component, unit.component_id).code
        for unit in db.scalars(
            select(BloodUnit).where(BloodUnit.donation_id == donation.id)
        ).all()
    }

    assert "PLT_RD" not in codes, "a component recorded as lost was still created"


def test_a_component_outside_the_recipe_is_refused(
    db, officer, phlebotomist, technologist, verifier, facility
):
    donation = _released_donation(
        db,
        officer,
        phlebotomist,
        technologist,
        verifier,
        facility,
        bag_type="DOUBLE",
    )

    with pytest.raises(ServiceError) as raised:
        processing.separate(
            db, technologist, donation_id=donation.id, produce=["PRBC", "FFP", "PLT_RD"]
        )

    assert raised.value.code == "NOT_IN_RECIPE"


def test_the_time_from_needle_to_spin_is_recorded(
    db, officer, phlebotomist, technologist, verifier, facility
):
    """A facility that keeps missing the platelet window has a scheduling
    problem, and it can only see that if the interval is stored."""

    donation = _released_donation(
        db, officer, phlebotomist, technologist, verifier, facility
    )
    record = processing.separate(db, technologist, donation_id=donation.id)

    assert record.minutes_from_collection is not None
    assert record.minutes_from_collection >= 0


# ---------------------------------------------------------------- permissions


def test_a_phlebotomist_cannot_separate_components(
    db, officer, phlebotomist, technologist, verifier, facility
):
    donation = _released_donation(
        db, officer, phlebotomist, technologist, verifier, facility
    )

    with pytest.raises(PermissionDenied):
        processing.separate(db, phlebotomist, donation_id=donation.id)


def test_separation_is_audited_with_what_was_lost(
    db, officer, phlebotomist, technologist, verifier, facility
):
    donation = _released_donation(
        db, officer, phlebotomist, technologist, verifier, facility
    )
    record = processing.separate(
        db,
        technologist,
        donation_id=donation.id,
        produce=["PRBC", "FFP"],
        losses={"PLT_RD": "LIPAEMIC"},
    )

    entry = db.scalars(
        select(AuditLog).where(AuditLog.entity_id == record.id).limit(1)
    ).first()

    assert entry is not None
    assert entry.action == "COMPONENTS_SEPARATED"

    context = (entry.after_json or {}).get("_context", {})

    assert context.get("losses", {}).get("PLT_RD") == "LIPAEMIC"
    assert set(context.get("produced", [])) == {"PRBC", "FFP"}


# ------------------------------------------------------------------ reporting


def test_the_yield_summary_separates_scheduling_loss_from_technique_loss(
    db, officer, phlebotomist, technologist, verifier, facility
):
    """A bank losing platelets to a missed window has a problem it can schedule
    away. One losing them to failed spins cannot fix that by rescheduling."""

    donation = _released_donation(
        db, officer, phlebotomist, technologist, verifier, facility
    )
    processing.separate(
        db,
        technologist,
        donation_id=donation.id,
        produce=["PRBC", "FFP"],
        losses={"PLT_RD": "FAILED_SPIN"},
    )

    summary = processing.yield_summary(db, facility.id)

    assert summary["separations"] >= 1
    assert summary["units_lost"] >= 1
    assert 0.0 < summary["yield_rate"] <= 1.0

    reasons = dict(summary["losses_by_reason"])

    assert "FAILED_SPIN" in reasons
