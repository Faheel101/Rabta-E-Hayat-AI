"""Lab testing and release: the control point where a bag becomes issuable.

Two rules carry almost all the weight here.

Nobody releases their own work — the technologist who recorded a result cannot
verify it. An audit trail that names one person for both is a trail that proves
nothing, and the generated history satisfies this only because the generator was
written to; these tests are about the service refusing.

And a reactive result has all of its consequences at once. There must be no
window in which a reactive unit is still issuable because somebody had not
clicked the next thing yet.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.clock import DEMO_DATETIME
from db.models import (
    AuditLog,
    BloodUnit,
    Donation,
    DonationTest,
    Donor,
    DonorDeferral,
    Facility,
    LabRun,
)
from services import lab
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


@pytest.fixture
def phlebotomist(facility):
    return Actor(
        user_id="lab-test-phleb",
        display_name="Lab Test Phlebotomist",
        role="PHLEBOTOMIST",
        facility_id=facility.id,
        organization_id=facility.organization_id,
    )


@pytest.fixture
def tester(facility):
    return Actor(
        user_id="lab-test-tech-a",
        display_name="Technologist A",
        role="LAB_TECHNOLOGIST",
        facility_id=facility.id,
        organization_id=facility.organization_id,
    )


@pytest.fixture
def verifier(facility):
    """A second, different technologist. The whole point of the control."""

    return Actor(
        user_id="lab-test-tech-b",
        display_name="Technologist B",
        role="LAB_TECHNOLOGIST",
        facility_id=facility.id,
        organization_id=facility.organization_id,
    )


def _collect(db, phlebotomist, facility) -> Donation:
    """A freshly collected donation, quarantined and awaiting the lab."""

    officer = Actor(
        user_id="lab-test-officer",
        display_name="Lab Test Officer",
        role="BLOOD_BANK_OFFICER",
        facility_id=facility.id,
        organization_id=facility.organization_id,
    )
    session_row = session_service.open_session(db, officer, session_type="IN_HOUSE")

    donor = screening_service.register_donor(
        db,
        officer,
        full_name="Lab Flow Donor",
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

    return screening_service.record_donation(
        db, phlebotomist, screening_id=draft.id
    )


def _run_full_panel(db, tester, donation, *, reactive_marker=None):
    """Record every required test, optionally making one reactive."""

    for marker in lab.required_tests():
        run = lab.open_run(
            db, tester, test_code=marker, kit_lot=f"LOT-{marker}", method="ELISA"
        )
        lab.record_results(
            db,
            tester,
            run_id=run.id,
            results={
                donation.id: "REACTIVE" if marker == reactive_marker else "NON_REACTIVE"
            },
        )


# ------------------------------------------------------------------ worklist


def test_a_collected_donation_appears_on_the_lab_worklist(
    db, phlebotomist, tester, facility
):
    donation = _collect(db, phlebotomist, facility)

    queue = lab.pending(db, tester)
    entry = next((row for row in queue if row.donation_id == donation.id), None)

    assert entry is not None, "a collected donation is not on the lab worklist"
    assert set(entry.outstanding) == set(lab.required_tests())
    assert not entry.is_complete


def test_the_worklist_shows_what_each_donation_still_needs(
    db, phlebotomist, tester, facility
):
    donation = _collect(db, phlebotomist, facility)

    run = lab.open_run(db, tester, test_code="HIV", kit_lot="LOT-1")
    lab.record_results(db, tester, run_id=run.id, results={donation.id: "NON_REACTIVE"})

    entry = next(
        row for row in lab.pending(db, tester) if row.donation_id == donation.id
    )

    assert "HIV" not in entry.outstanding
    assert "HCV" in entry.outstanding


# ------------------------------------------------------------------ lab runs


def test_a_run_carries_the_kit_lot_so_a_recall_is_one_join_away(
    db, tester, phlebotomist, facility
):
    """When a lot is recalled the question is which donations it touched."""

    donation = _collect(db, phlebotomist, facility)

    run = lab.open_run(db, tester, test_code="HCV", kit_lot="LOT-RECALLED-99")
    lab.record_results(db, tester, run_id=run.id, results={donation.id: "NON_REACTIVE"})

    touched = db.scalars(
        select(DonationTest.donation_id)
        .join(LabRun, LabRun.id == DonationTest.lab_run_id)
        .where(LabRun.kit_lot == "LOT-RECALLED-99")
    ).all()

    assert donation.id in touched


def test_an_expired_kit_cannot_be_used(db, tester):
    expired = DEMO_DATETIME.date().replace(year=DEMO_DATETIME.year - 1)

    with pytest.raises(ServiceError) as raised:
        lab.open_run(db, tester, test_code="HIV", kit_lot="OLD", kit_expiry=expired)

    assert raised.value.code == "KIT_EXPIRED"


def test_a_test_outside_the_configured_panel_is_refused(db, tester):
    with pytest.raises(ServiceError) as raised:
        lab.open_run(db, tester, test_code="NOT_A_REAL_ASSAY")

    assert raised.value.code == "UNKNOWN_TEST"


def test_failed_controls_invalidate_the_plate(db, tester, phlebotomist, facility):
    """A plate whose control did not behave cannot be interpreted, however clean
    the sample wells look."""

    donation = _collect(db, phlebotomist, facility)
    run = lab.open_run(db, tester, test_code="HIV", kit_lot="LOT-BAD")

    lab.record_controls(
        db, tester, run_id=run.id, controls_valid=False, note="Positive control flat."
    )

    db.refresh(run)

    assert run.status == "INVALIDATED"

    with pytest.raises(ServiceError) as raised:
        lab.record_results(
            db, tester, run_id=run.id, results={donation.id: "NON_REACTIVE"}
        )

    assert raised.value.code == "CONTROLS_FAILED"


def test_a_result_is_never_silently_overwritten(db, tester, phlebotomist, facility):
    """Overwriting is how a reactive result disappears. A repeat needs its own
    run and its own record."""

    donation = _collect(db, phlebotomist, facility)

    first = lab.open_run(db, tester, test_code="HIV", kit_lot="LOT-1")
    lab.record_results(db, tester, run_id=first.id, results={donation.id: "REACTIVE"})

    second = lab.open_run(db, tester, test_code="HIV", kit_lot="LOT-2")
    lab.record_results(
        db, tester, run_id=second.id, results={donation.id: "NON_REACTIVE"}
    )

    results = db.scalars(
        select(DonationTest).where(
            DonationTest.donation_id == donation.id,
            DonationTest.test_code == "HIV",
            DonationTest.test_group == "TTI",
        )
    ).all()

    assert len(results) == 1
    assert results[0].is_reactive is True, "a reactive result was overwritten"


# -------------------------------------------------------- the reactive cascade


def test_a_reactive_result_discards_the_units_immediately(
    db, tester, phlebotomist, facility
):
    """No window in which a reactive unit is still issuable."""

    donation = _collect(db, phlebotomist, facility)

    run = lab.open_run(db, tester, test_code="HCV", kit_lot="LOT-R")
    lab.record_results(db, tester, run_id=run.id, results={donation.id: "REACTIVE"})

    units = db.scalars(
        select(BloodUnit).where(BloodUnit.donation_id == donation.id)
    ).all()

    assert units

    for unit in units:
        assert unit.status == "DISCARDED"
        assert unit.screening_status == "FAILED"
        assert unit.discard_reason == "TTI_REACTIVE_HCV"
        assert unit.discarded_at is not None


def test_a_reactive_result_quarantines_the_donation(
    db, tester, phlebotomist, facility
):
    donation = _collect(db, phlebotomist, facility)

    run = lab.open_run(db, tester, test_code="HIV", kit_lot="LOT-R")
    lab.record_results(db, tester, run_id=run.id, results={donation.id: "REACTIVE"})

    db.refresh(donation)

    assert donation.status == "QUARANTINED"
    assert donation.released_at is None


def test_a_reactive_result_defers_the_donor_without_labelling_them_infected(
    db, tester, phlebotomist, facility
):
    """A reactive screen is not a diagnosis. Roughly half of those flagged are
    not infected, and permanently labelling them is a real harm."""

    donation = _collect(db, phlebotomist, facility)

    run = lab.open_run(db, tester, test_code="HBSAG", kit_lot="LOT-R")
    lab.record_results(db, tester, run_id=run.id, results={donation.id: "REACTIVE"})

    donor = db.get(Donor, donation.donor_id)
    db.refresh(donor)

    assert donor.availability_status == "AWAITING_TTI_CONFIRMATION"
    assert donor.is_permanently_deferred is False, (
        "a screening result permanently labelled a donor as infected"
    )

    deferral = db.scalars(
        select(DonorDeferral).where(
            DonorDeferral.donor_id == donor.id,
            DonorDeferral.lifted_at.is_(None),
        )
    ).first()

    assert deferral is not None
    assert deferral.reason_code.startswith("TTI_AWAITING")


def test_a_reactive_result_raises_the_confirmatory_test(
    db, tester, phlebotomist, facility
):
    """Raised automatically rather than left for somebody to remember."""

    donation = _collect(db, phlebotomist, facility)

    run = lab.open_run(db, tester, test_code="SYPHILIS", kit_lot="LOT-R")
    lab.record_results(db, tester, run_id=run.id, results={donation.id: "REACTIVE"})

    confirmatory = db.scalars(
        select(DonationTest).where(
            DonationTest.donation_id == donation.id,
            DonationTest.test_group == "TTI_CONFIRMATORY",
        )
    ).first()

    assert confirmatory is not None
    assert confirmatory.result == "PENDING"
    assert confirmatory.method != "ELISA", "confirmation must be a different assay"


# ------------------------------------------------------------------- release


def test_nobody_can_release_their_own_results(db, tester, phlebotomist, facility):
    """The rule the whole module exists around."""

    donation = _collect(db, phlebotomist, facility)
    _run_full_panel(db, tester, donation)

    with pytest.raises(ServiceError) as raised:
        lab.release(db, tester, donation_id=donation.id)

    assert raised.value.code == "SELF_RELEASE"


def test_a_second_technologist_can_release(
    db, tester, verifier, phlebotomist, facility
):
    donation = _collect(db, phlebotomist, facility)
    _run_full_panel(db, tester, donation)

    released = lab.release(db, verifier, donation_id=donation.id)

    assert released.status == "RELEASED"
    assert released.released_by == verifier.display_name

    units = db.scalars(
        select(BloodUnit).where(BloodUnit.donation_id == donation.id)
    ).all()

    for unit in units:
        assert unit.status == "AVAILABLE"
        assert unit.screening_status == "PASSED"


def test_release_records_both_names(db, tester, verifier, phlebotomist, facility):
    """A trail naming one person for both halves proves nothing."""

    donation = _collect(db, phlebotomist, facility)
    _run_full_panel(db, tester, donation)
    lab.release(db, verifier, donation_id=donation.id)

    results = db.scalars(
        select(DonationTest).where(
            DonationTest.donation_id == donation.id,
            DonationTest.test_group == "TTI",
        )
    ).all()

    for row in results:
        assert row.tested_by == tester.display_name
        assert row.verified_by == verifier.display_name
        assert row.verified_by != row.tested_by


def test_an_incomplete_panel_cannot_be_released(
    db, tester, verifier, phlebotomist, facility
):
    donation = _collect(db, phlebotomist, facility)

    run = lab.open_run(db, tester, test_code="HIV", kit_lot="LOT-1")
    lab.record_results(db, tester, run_id=run.id, results={donation.id: "NON_REACTIVE"})

    with pytest.raises(ServiceError) as raised:
        lab.release(db, verifier, donation_id=donation.id)

    assert raised.value.code == "PANEL_INCOMPLETE"


def test_a_reactive_donation_cannot_be_released(
    db, tester, verifier, phlebotomist, facility
):
    donation = _collect(db, phlebotomist, facility)
    _run_full_panel(db, tester, donation, reactive_marker="HCV")

    with pytest.raises(ServiceError) as raised:
        lab.release(db, verifier, donation_id=donation.id)

    assert raised.value.code in ("QUARANTINED", "REACTIVE_RESULT")


def test_results_from_an_invalidated_plate_cannot_be_released(
    db, tester, verifier, phlebotomist, facility
):
    """Controls that failed after the fact must still block release."""

    donation = _collect(db, phlebotomist, facility)
    _run_full_panel(db, tester, donation)

    run_id = db.scalar(
        select(DonationTest.lab_run_id).where(
            DonationTest.donation_id == donation.id,
            DonationTest.test_code == "HIV",
        )
    )
    lab.record_controls(db, tester, run_id=run_id, controls_valid=False)

    with pytest.raises(ServiceError) as raised:
        lab.release(db, verifier, donation_id=donation.id)

    assert raised.value.code == "CONTROLS_FAILED"


def test_a_phlebotomist_cannot_release(db, tester, phlebotomist, facility):
    donation = _collect(db, phlebotomist, facility)
    _run_full_panel(db, tester, donation)

    with pytest.raises(PermissionDenied):
        lab.release(db, phlebotomist, donation_id=donation.id)


def test_a_phlebotomist_cannot_record_results(db, phlebotomist):
    with pytest.raises(PermissionDenied):
        lab.open_run(db, phlebotomist, test_code="HIV")


def test_release_is_audited_with_both_names(
    db, tester, verifier, phlebotomist, facility
):
    donation = _collect(db, phlebotomist, facility)
    _run_full_panel(db, tester, donation)
    lab.release(db, verifier, donation_id=donation.id)

    entry = db.scalars(
        select(AuditLog).where(
            AuditLog.entity_id == donation.id,
            AuditLog.action == "DONATION_RELEASED",
        )
    ).first()

    assert entry is not None

    context = (entry.after_json or {}).get("_context", {})

    assert tester.display_name in context.get("tested_by", [])
    assert context.get("verified_by") == verifier.display_name
    assert context.get("units_released", 0) > 0


# ------------------------------------------------------------- confirmation


def test_a_positive_confirmation_defers_the_donor_permanently(
    db, tester, verifier, phlebotomist, facility
):
    donation = _collect(db, phlebotomist, facility)
    run = lab.open_run(db, tester, test_code="HCV", kit_lot="LOT-R")
    lab.record_results(db, tester, run_id=run.id, results={donation.id: "REACTIVE"})

    lab.record_confirmation(
        db, verifier, donation_id=donation.id, marker="HCV", confirmed=True
    )

    donor = db.get(Donor, donation.donor_id)
    db.refresh(donor)

    assert donor.is_permanently_deferred is True
    assert donor.availability_status == "PERMANENTLY_DEFERRED"


def test_a_negative_confirmation_does_not_clear_the_donor_outright(
    db, tester, verifier, phlebotomist, facility
):
    """Something made the screen react. They are re-tested before reinstatement,
    not waved straight back to the chair."""

    donation = _collect(db, phlebotomist, facility)
    run = lab.open_run(db, tester, test_code="HCV", kit_lot="LOT-R")
    lab.record_results(db, tester, run_id=run.id, results={donation.id: "REACTIVE"})

    lab.record_confirmation(
        db, verifier, donation_id=donation.id, marker="HCV", confirmed=False
    )

    donor = db.get(Donor, donation.donor_id)
    db.refresh(donor)

    assert donor.is_permanently_deferred is False
    assert donor.availability_status == "TEMPORARILY_DEFERRED"
    assert donor.deferred_until is not None
    assert donor.deferred_until > DEMO_DATETIME.date()


def test_a_confirmation_cannot_be_recorded_twice(
    db, tester, verifier, phlebotomist, facility
):
    donation = _collect(db, phlebotomist, facility)
    run = lab.open_run(db, tester, test_code="HIV", kit_lot="LOT-R")
    lab.record_results(db, tester, run_id=run.id, results={donation.id: "REACTIVE"})

    lab.record_confirmation(
        db, verifier, donation_id=donation.id, marker="HIV", confirmed=False
    )

    with pytest.raises(ServiceError) as raised:
        lab.record_confirmation(
            db, verifier, donation_id=donation.id, marker="HIV", confirmed=True
        )

    assert raised.value.code == "ALREADY_CONFIRMED"
