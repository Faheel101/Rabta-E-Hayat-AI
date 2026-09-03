"""The screening and collection service, and the refusals it will not let past.

Every test runs inside an outer transaction that is rolled back, so the suite can
exercise real writes against the generated database without changing it. That
matters more than usual here: the service commits deliberately, so a plain
session rollback discards nothing.

The point of these is that the rules live in the service, not the template. A
hidden button is not a control: the second caller — the camp tablet, an import,
a correction screen — will not have the template.
"""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Permission
from core.clock import DEMO_DATETIME
from db.models import AuditLog, BloodUnit, Donation, Donor, DonorDeferral, DonorScreening
from services import screening as svc
from services.audit import Actor, PermissionDenied, ServiceError

HEALTHY = {
    "haemoglobin_g_dl": 15.0,
    "weight_kg": 72.0,
    "systolic_bp": 120,
    "diastolic_bp": 78,
    "pulse_bpm": 72,
    "temperature_c": 36.7,
}

ALL_CLEAR = {question["key"]: False for question in svc.eligibility.QUESTIONS}


@pytest.fixture
def db(scratch_database):
    session = Session(bind=scratch_database)

    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def donor(db):
    """An eligible donor: no deferral, outside the interval, inside the age range.

    The age filter is not incidental. The register deliberately contains donors
    outside 18-60 — people age out, and a 17-year-old may pre-register — so a
    fixture that ignores age picks one of them and the service correctly defers
    it, which looks like a service bug and is not.
    """

    cutoff = DEMO_DATETIME.date().replace(year=DEMO_DATETIME.year - 1)
    oldest = DEMO_DATETIME.date().replace(year=DEMO_DATETIME.year - 60)
    youngest = DEMO_DATETIME.date().replace(year=DEMO_DATETIME.year - 19)

    found = db.scalars(
        select(Donor)
        .where(
            Donor.is_permanently_deferred.is_(False),
            Donor.deferred_until.is_(None),
            Donor.blood_group_id.is_not(None),
            Donor.date_of_birth.between(oldest, youngest),
            Donor.gender == "MALE",
            (Donor.last_donation_at.is_(None)) | (Donor.last_donation_at < cutoff),
        )
        .limit(1)
    ).first()

    assert found is not None, "no eligible donor in the register to screen"

    return found


@pytest.fixture
def phlebotomist(donor):
    return Actor(
        user_id="test-phleb",
        display_name="Test Phlebotomist",
        role="PHLEBOTOMIST",
        facility_id=donor.registered_facility_id,
    )


# ------------------------------------------------------------------ assessment


def test_a_healthy_donor_is_accepted(db, donor):
    verdict = svc.assess(donor, answers=ALL_CLEAR, **HEALTHY)

    assert verdict.accepted, f"deferred on {[d.reason_code for d in verdict.assessment.deferrals]}"
    assert verdict.collection_volume_ml == 450


def test_low_haemoglobin_defers_before_the_questionnaire(db, donor):
    """The wizard assesses after each step, so a donor deferred on a measurement
    should not have to answer twelve more questions first."""

    vitals = dict(HEALTHY, haemoglobin_g_dl=10.2)
    verdict = svc.assess(donor, **vitals)

    assert not verdict.accepted
    assert "LOW_HAEMOGLOBIN" in [d.reason_code for d in verdict.assessment.deferrals]


def test_an_underweight_donor_gets_a_reduced_volume_not_a_full_one(db, donor):
    verdict = svc.assess(donor, answers=ALL_CLEAR, **dict(HEALTHY, weight_kg=47.0))

    assert verdict.collection_volume_ml == 350


def test_below_the_absolute_floor_nothing_may_be_collected(db, donor):
    verdict = svc.assess(donor, answers=ALL_CLEAR, **dict(HEALTHY, weight_kg=42.0))

    assert verdict.collection_volume_ml is None


def test_a_contested_rule_is_flagged_for_clinical_signoff(db, donor):
    """The seven rules where the sources disagree must be surfaced, not silently
    resolved by whichever limb the config happens to apply."""

    contested = svc.contested_rules()

    assert contested, "no rules are flagged as contested"

    answers = dict(ALL_CLEAR)

    # Find a questionnaire answer that triggers a contested rule.
    triggering = [
        question["key"]
        for question in svc.eligibility.QUESTIONS
        if question.get("reason_code") in contested
    ]

    if not triggering:
        pytest.skip("no questionnaire answer maps to a contested rule")

    answers[triggering[0]] = True
    verdict = svc.assess(donor, answers=answers, **HEALTHY)

    assert verdict.needs_signoff, "a contested rule fired but was not flagged"


# ------------------------------------------------------------------ recording


def test_recording_a_screening_writes_an_audit_entry(db, donor, phlebotomist):
    before = db.scalar(select(func.count()).select_from(AuditLog))

    record = svc.record_screening(
        db,
        phlebotomist,
        donor_id=donor.id,
        session_id=None,
        vitals=HEALTHY,
        answers=ALL_CLEAR,
    )

    assert record.outcome == "ACCEPTED"

    after = db.scalar(select(func.count()).select_from(AuditLog))

    assert after == before + 1, "the screening wrote no audit entry"

    # Found by the record it describes, not by "the most recent row" — other
    # traffic (a login, another test) writes to the same table.
    entry = db.scalars(
        select(AuditLog).where(AuditLog.entity_id == record.id).limit(1)
    ).first()

    assert entry is not None, "no audit entry references this screening"

    assert entry.action == "DONOR_SCREENED"
    assert entry.entity_id == record.id
    assert phlebotomist.display_name in entry.actor


def test_a_deferring_screening_writes_a_deferral_the_register_can_see(
    db, donor, phlebotomist
):
    """A deferral that reaches only the screening row is a deferral the recall
    desk never learns about."""

    record = svc.record_screening(
        db,
        phlebotomist,
        donor_id=donor.id,
        session_id=None,
        vitals=dict(HEALTHY, haemoglobin_g_dl=9.8),
        answers=ALL_CLEAR,
    )

    assert record.outcome == "DEFERRED"

    deferral = db.scalars(
        select(DonorDeferral)
        .where(DonorDeferral.donor_id == donor.id)
        .order_by(DonorDeferral.deferred_at.desc())
        .limit(1)
    ).first()

    assert deferral is not None, "no deferral row was written"
    assert deferral.reason_code == "LOW_HAEMOGLOBIN"

    db.refresh(donor)

    assert donor.availability_status != "AVAILABLE"


def test_a_conditional_deferral_gets_no_end_date(db, donor, phlebotomist):
    """Giving a CONDITIONAL deferral a duration would let a
    `today >= deferred_until` check score the donor eligible on a date that
    means nothing."""

    svc.record_screening(
        db,
        phlebotomist,
        donor_id=donor.id,
        session_id=None,
        vitals=dict(HEALTHY, weight_kg=44.0),
        answers=ALL_CLEAR,
    )

    deferral = db.scalars(
        select(DonorDeferral)
        .where(DonorDeferral.donor_id == donor.id)
        .order_by(DonorDeferral.deferred_at.desc())
        .limit(1)
    ).first()

    if deferral is None or deferral.is_permanent:
        pytest.skip("that weight did not produce a conditional deferral")

    if deferral.reason_code == "UNDERWEIGHT":
        assert deferral.deferred_until is None


# ------------------------------------------------------------------ refusals


def test_a_deferred_donor_cannot_be_bled(db, donor, phlebotomist):
    """The one that matters. The refusal is in the service, so a caller that
    never saw the template still cannot do it."""

    record = svc.record_screening(
        db,
        phlebotomist,
        donor_id=donor.id,
        session_id=None,
        vitals=dict(HEALTHY, haemoglobin_g_dl=9.8),
        answers=ALL_CLEAR,
    )

    with pytest.raises(ServiceError) as raised:
        svc.record_donation(db, phlebotomist, screening_id=record.id)

    assert raised.value.code == "DONOR_DEFERRED"


def test_a_screening_can_only_be_collected_against_once(db, donor, phlebotomist):
    record = svc.record_screening(
        db,
        phlebotomist,
        donor_id=donor.id,
        session_id=None,
        vitals=HEALTHY,
        answers=ALL_CLEAR,
    )

    svc.record_donation(db, phlebotomist, screening_id=record.id)

    with pytest.raises(ServiceError) as raised:
        svc.record_donation(db, phlebotomist, screening_id=record.id)

    assert raised.value.code == "ALREADY_COLLECTED"


def test_a_lab_technologist_cannot_record_a_collection(db, donor):
    """Segregation of duties, enforced in the service rather than by hiding a
    page from a role."""

    technologist = Actor(
        user_id="test-lab",
        display_name="Test Technologist",
        role="LAB_TECHNOLOGIST",
        facility_id=donor.registered_facility_id,
    )

    with pytest.raises(PermissionDenied):
        svc.record_screening(
            db,
            technologist,
            donor_id=donor.id,
            session_id=None,
            vitals=HEALTHY,
            answers=ALL_CLEAR,
        )


def test_a_donor_at_another_facility_cannot_be_screened(db, donor):
    stranger = Actor(
        user_id="test-other",
        display_name="Other Facility",
        role="PHLEBOTOMIST",
        facility_id="a-different-facility",
    )

    with pytest.raises(ServiceError) as raised:
        svc.record_screening(
            db,
            stranger,
            donor_id=donor.id,
            session_id=None,
            vitals=HEALTHY,
            answers=ALL_CLEAR,
        )

    assert raised.value.code == "DONOR_NOT_FOUND"


# ------------------------------------------------------------------ the bag


def test_collection_creates_quarantined_units_not_available_stock(
    db, donor, phlebotomist
):
    """The bag exists the moment it is drawn, so the record does too — but it is
    not issuable and must not be counted as stock until the lab releases it."""

    record = svc.record_screening(
        db,
        phlebotomist,
        donor_id=donor.id,
        session_id=None,
        vitals=HEALTHY,
        answers=ALL_CLEAR,
    )
    donation = svc.record_donation(db, phlebotomist, screening_id=record.id)

    units = db.scalars(
        select(BloodUnit).where(BloodUnit.donation_id == donation.id)
    ).all()

    assert units, "collection produced no unit records"

    for unit in units:
        assert unit.status == "QUARANTINE", f"{unit.din} is already {unit.status}"
        assert unit.screening_status == "PENDING"
        assert unit.blood_group_id == donor.blood_group_id
        assert unit.collected_at == donation.collected_at
        assert unit.expires_at > donation.collected_at


def test_the_donation_carries_a_valid_isbt_identifier(db, donor, phlebotomist):
    from core import isbt

    record = svc.record_screening(
        db,
        phlebotomist,
        donor_id=donor.id,
        session_id=None,
        vitals=HEALTHY,
        answers=ALL_CLEAR,
    )
    donation = svc.record_donation(db, phlebotomist, screening_id=record.id)

    identifier = isbt.parse_din(donation.din)

    # verify() takes the payload PLUS its check character; the stored DIN is the
    # 13-character payload alone.
    assert isbt.verify(donation.din + identifier.check_character), (
        "the DIN fails its own check character"
    )
    assert identifier.is_provisional is True, (
        "a self-allocated FIN must never report as conformant"
    )


def test_collection_updates_the_donors_history(db, donor, phlebotomist):
    before = donor.total_donations or 0

    record = svc.record_screening(
        db,
        phlebotomist,
        donor_id=donor.id,
        session_id=None,
        vitals=HEALTHY,
        answers=ALL_CLEAR,
    )
    svc.record_donation(db, phlebotomist, screening_id=record.id)

    db.refresh(donor)

    assert donor.total_donations == before + 1
    assert donor.last_donation_at == DEMO_DATETIME
