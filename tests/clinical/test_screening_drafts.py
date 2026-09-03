"""The draft lifecycle, and the rule that drafts never count as screenings.

A screening row is created when the donor is identified so a closed tab does not
lose the work. The price is DRAFT rows sitting in a table other things count, and
the whole point of these tests is that the price is actually paid — every query
that reports "screenings" has to exclude them, and half-entered vitals must never
appear on a donor's clinical record.
"""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.clock import DEMO_DATETIME
from db.models import AuditLog, Donor, DonorScreening, Facility
from services import screening as svc
from services import sessions as sess
from services.audit import Actor, ServiceError

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

    assert found is not None, "no eligible donor to screen"

    return found


@pytest.fixture
def actor(donor, db):
    facility = db.get(Facility, donor.registered_facility_id)

    return Actor(
        user_id="test-phleb",
        display_name="Test Phlebotomist",
        role="PHLEBOTOMIST",
        facility_id=donor.registered_facility_id,
        organization_id=facility.organization_id,
    )


@pytest.fixture
def session_row(db, actor):
    officer = Actor(
        user_id="test-officer",
        display_name="Test Officer",
        role="BLOOD_BANK_OFFICER",
        facility_id=actor.facility_id,
        organization_id=actor.organization_id,
    )

    return sess.open_session(db, officer, session_type="IN_HOUSE")


# ------------------------------------------------------------------ lifecycle


def test_starting_a_screening_creates_a_draft(db, actor, donor, session_row):
    draft = svc.start_screening(
        db, actor, donor_id=donor.id, session_id=session_row.id
    )

    assert draft.outcome == svc.DRAFT
    assert draft.haemoglobin_g_dl is None


def test_reopening_resumes_rather_than_starting_a_second_record(
    db, actor, donor, session_row
):
    first = svc.start_screening(db, actor, donor_id=donor.id, session_id=session_row.id)
    second = svc.start_screening(db, actor, donor_id=donor.id, session_id=session_row.id)

    assert first.id == second.id, "a second draft was opened for the same donor"


def test_the_verdict_updates_as_each_step_is_saved(db, actor, donor, session_row):
    """A donor deferred on their haemoglobin should learn that before answering
    twelve more questions."""

    draft = svc.start_screening(db, actor, donor_id=donor.id, session_id=session_row.id)

    _, verdict = svc.save_draft(
        db, actor, screening_id=draft.id, vitals={"haemoglobin_g_dl": 9.6}
    )

    assert not verdict.accepted
    assert "LOW_HAEMOGLOBIN" in [d.reason_code for d in verdict.assessment.deferrals]

    # Correcting it clears the deferral, without restarting the screening.
    _, verdict = svc.save_draft(
        db, actor, screening_id=draft.id, vitals=dict(HEALTHY)
    )

    assert "LOW_HAEMOGLOBIN" not in [
        d.reason_code for d in verdict.assessment.deferrals
    ]


def test_answers_accumulate_across_steps(db, actor, donor, session_row):
    draft = svc.start_screening(db, actor, donor_id=donor.id, session_id=session_row.id)

    svc.save_draft(db, actor, screening_id=draft.id, answers={"unwell_today": False})
    record, _ = svc.save_draft(
        db, actor, screening_id=draft.id, answers={"malaria_illness": False}
    )

    assert record.questionnaire_json.get("unwell_today") is False
    assert record.questionnaire_json.get("malaria_illness") is False, (
        "saving a later step discarded an earlier answer"
    )


def test_a_screening_cannot_be_completed_without_haemoglobin_and_weight(
    db, actor, donor, session_row
):
    """Both decide eligibility, and weight also decides how much may be taken."""

    draft = svc.start_screening(db, actor, donor_id=donor.id, session_id=session_row.id)

    with pytest.raises(ServiceError) as raised:
        svc.finalise_screening(db, actor, screening_id=draft.id)

    assert raised.value.code == "INCOMPLETE"


def test_finalising_produces_a_real_screening(db, actor, donor, session_row):
    draft = svc.start_screening(db, actor, donor_id=donor.id, session_id=session_row.id)
    svc.save_draft(db, actor, screening_id=draft.id, vitals=HEALTHY, answers=ALL_CLEAR)

    record, verdict = svc.finalise_screening(db, actor, screening_id=draft.id)

    assert record.outcome == "ACCEPTED"
    assert verdict.collection_volume_ml == 450


def test_a_completed_screening_cannot_be_edited(db, actor, donor, session_row):
    draft = svc.start_screening(db, actor, donor_id=donor.id, session_id=session_row.id)
    svc.save_draft(db, actor, screening_id=draft.id, vitals=HEALTHY, answers=ALL_CLEAR)
    svc.finalise_screening(db, actor, screening_id=draft.id)

    with pytest.raises(ServiceError) as raised:
        svc.save_draft(db, actor, screening_id=draft.id, vitals={"weight_kg": 40.0})

    assert raised.value.code == "ALREADY_COMPLETE"


def test_abandoning_keeps_the_record_rather_than_deleting_it(
    db, actor, donor, session_row
):
    """A donor who presented and walked off is a real event, and at a camp the
    reason is often the useful part."""

    draft = svc.start_screening(db, actor, donor_id=donor.id, session_id=session_row.id)

    record = svc.abandon_draft(
        db, actor, screening_id=draft.id, reason="Donor left before the questionnaire."
    )

    assert record.outcome == svc.ABANDONED
    assert db.get(DonorScreening, draft.id) is not None
    assert "left before" in (record.notes or "")


def test_open_drafts_are_listed_so_the_chair_can_resume_one(
    db, actor, donor, session_row
):
    draft = svc.start_screening(db, actor, donor_id=donor.id, session_id=session_row.id)

    open_now = svc.open_drafts(db, session_id=session_row.id)

    assert draft.id in {row.id for row in open_now}

    svc.abandon_draft(db, actor, screening_id=draft.id)

    assert draft.id not in {
        row.id for row in svc.open_drafts(db, session_id=session_row.id)
    }


# ------------------------------------------------------- drafts are not counted


def test_a_draft_does_not_count_as_a_screening_on_the_session(
    db, actor, donor, session_row
):
    before = sess.session_summary(db, session_row.id)["screened"]

    svc.start_screening(db, actor, donor_id=donor.id, session_id=session_row.id)

    after = sess.session_summary(db, session_row.id)["screened"]

    assert after == before, "an unfinished draft was counted as a screening"


def test_a_draft_never_appears_on_the_donors_clinical_record(
    db, actor, donor, session_row
):
    """Half-entered vitals on a clinical record would read as a real reading."""

    svc.start_screening(db, actor, donor_id=donor.id, session_id=session_row.id)

    shown = db.execute(
        select(DonorScreening.outcome).where(
            DonorScreening.donor_id == donor.id,
            DonorScreening.outcome.in_(svc.FINAL_OUTCOMES),
        )
    ).all()

    assert svc.DRAFT not in {row[0] for row in shown}
    assert svc.ABANDONED not in {row[0] for row in shown}


def test_every_step_of_the_draft_is_audited(db, actor, donor, session_row):
    draft = svc.start_screening(db, actor, donor_id=donor.id, session_id=session_row.id)
    svc.save_draft(db, actor, screening_id=draft.id, vitals=HEALTHY, answers=ALL_CLEAR)
    svc.finalise_screening(db, actor, screening_id=draft.id)

    actions = {
        row[0]
        for row in db.execute(
            select(AuditLog.action).where(AuditLog.entity_id == draft.id)
        ).all()
    }

    assert {"SCREENING_STARTED", "SCREENING_UPDATED", "DONOR_SCREENED"} <= actions
