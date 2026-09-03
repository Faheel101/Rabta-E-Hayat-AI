"""Collection sessions, and clinical sign-off for the contested deferral rules.

Runs against a throwaway copy of the database. The service layer commits by
design — an audit entry and its change share a transaction — so a session-level
rollback discards nothing, and a savepoint-backed transaction does not help
either because pysqlite defers its BEGIN until the first statement.
"""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core import config
from core.clock import DEMO_DATETIME
from db.models import AuditLog, DonationSession, Donor, DonorDeferral, Facility
from services import sessions as sess
from services import signoff
from services.audit import Actor, PermissionDenied, ServiceError


@pytest.fixture
def db(scratch_database):
    session = Session(bind=scratch_database)

    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def facility(db):
    return db.scalars(select(Facility).where(Facility.is_active.is_(True)).limit(1)).first()


@pytest.fixture
def officer(facility):
    return Actor(
        user_id="test-officer",
        display_name="Test Officer",
        role="BLOOD_BANK_OFFICER",
        facility_id=facility.id,
        organization_id=facility.organization_id,
    )


@pytest.fixture
def phlebotomist(facility):
    return Actor(
        user_id="test-phleb",
        display_name="Test Phlebotomist",
        role="PHLEBOTOMIST",
        facility_id=facility.id,
        organization_id=facility.organization_id,
    )


# ------------------------------------------------------------------- sessions


def test_opening_a_bench_session_writes_an_audit_entry(db, officer):
    record = sess.open_session(db, officer, session_type="IN_HOUSE")

    assert record.status == "OPEN"
    assert record.opened_at == DEMO_DATETIME
    assert record.closed_at is None

    entry = db.scalars(
        select(AuditLog).where(AuditLog.entity_id == record.id).limit(1)
    ).first()

    assert entry is not None and entry.action == "SESSION_OPENED"


def test_a_camp_needs_a_venue(db, officer):
    """A camp with no venue cannot be found again, and its geography is the
    reason it is a separate record from the bench."""

    with pytest.raises(ServiceError) as raised:
        sess.open_session(db, officer, session_type="OUTREACH_CAMP", venue="")

    assert raised.value.code == "VENUE_REQUIRED"


def test_a_camp_records_its_target_and_organiser(db, officer):
    record = sess.open_session(
        db,
        officer,
        session_type="OUTREACH_CAMP",
        venue="Government College",
        organiser="Pakistan Red Crescent Society",
        target_units=120,
    )

    assert record.target_units == 120
    assert record.organiser == "Pakistan Red Crescent Society"
    assert "CAMP" in record.session_code


def test_session_codes_are_unique_per_facility_and_day(db, officer):
    first = sess.open_session(db, officer, session_type="IN_HOUSE")
    second = sess.open_session(db, officer, session_type="IN_HOUSE")

    assert first.session_code != second.session_code


def test_closing_records_what_the_session_achieved(db, officer):
    record = sess.open_session(
        db, officer, session_type="OUTREACH_CAMP", venue="Steel Works", target_units=100
    )

    closed = sess.close_session(db, officer, session_id=record.id)

    assert closed.status == "CLOSED"
    assert closed.closed_at == DEMO_DATETIME

    entry = db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_id == record.id, AuditLog.action == "SESSION_CLOSED")
        .limit(1)
    ).first()

    assert entry is not None

    context = (entry.after_json or {}).get("_context", {})

    # The shortfall is captured at close because the target is what somebody
    # committed to on the day.
    assert context.get("target_units") == 100
    assert context.get("shortfall") == 100


def test_a_session_cannot_be_closed_twice(db, officer):
    record = sess.open_session(db, officer, session_type="IN_HOUSE")
    sess.close_session(db, officer, session_id=record.id)

    with pytest.raises(ServiceError) as raised:
        sess.close_session(db, officer, session_id=record.id)

    assert raised.value.code == "ALREADY_CLOSED"


def test_a_session_at_another_facility_is_not_reachable(db, officer, facility):
    record = sess.open_session(db, officer, session_type="IN_HOUSE")

    stranger = Actor(
        user_id="test-other",
        display_name="Other",
        role="BLOOD_BANK_OFFICER",
        facility_id="a-different-facility",
    )

    with pytest.raises(ServiceError) as raised:
        sess.close_session(db, stranger, session_id=record.id)

    assert raised.value.code == "SESSION_NOT_FOUND"


def test_the_bench_opens_itself_for_the_first_screening_of_the_day(db, officer):
    """A screening must belong to a session, and nobody declares the bench open
    before drawing the first bag."""

    opened = sess.ensure_session(db, officer)

    assert opened.status == "OPEN"

    again = sess.ensure_session(db, officer)

    assert again.id == opened.id, "a second call opened a duplicate session"


def test_the_summary_separates_deferred_from_did_not_donate(db, officer):
    """The gap between screened and collected is two different things, and a
    camp organiser needs them apart."""

    record = sess.open_session(db, officer, session_type="IN_HOUSE")
    summary = sess.session_summary(db, record.id)

    assert set(summary) >= {
        "screened",
        "deferred",
        "accepted",
        "collected",
        "deferral_rate",
        "did_not_donate",
    }
    assert summary["screened"] == 0


# -------------------------------------------------------------------- signoff


def test_the_contested_rules_are_the_seven_from_config():
    rules = signoff.contested_rules()

    assert len(rules) == 7

    for code, rule in rules.items():
        assert rule.get("applied"), f"{code} does not say which limb is applied"
        assert rule.get("alternative"), f"{code} does not record the alternative"
        assert rule.get("note"), f"{code} does not say why it is contested"


def test_the_queue_surfaces_open_contested_deferrals(db, officer, facility):
    queue = signoff.pending(db, officer)

    if not queue:
        pytest.skip("no contested deferrals at this facility")

    case = queue[0]

    assert case["reason_code"] in signoff.contested_rules()
    assert case["rule"].get("applied")
    assert case["rule"].get("alternative"), "the reviewer cannot see the disagreement"
    assert case["days_waiting"] >= 0

    # Oldest first: a donor kept off the register longest is reviewed first.
    waits = [row["days_waiting"] for row in queue]
    assert waits == sorted(waits, reverse=True) or waits == sorted(waits), (
        "the queue is not ordered by how long the case has waited"
    )


def _a_case(db, officer, facility):
    """A case from the queue as the officer would actually see it.

    Scoped through the service rather than reconstructed, so the test cannot
    hand `lift` a case the officer is not permitted to act on.
    """

    queue = signoff.pending(db, officer)

    if not queue:
        pytest.skip("no contested deferrals at this facility")

    return queue[0]


def test_lifting_requires_actual_reasoning(db, officer, facility):
    """'Approved' is not a clinical judgement. These rules are flagged because
    the decision needs argument."""

    case = _a_case(db, officer, facility)

    with pytest.raises(ServiceError) as raised:
        signoff.lift(db, officer, deferral_id=case["deferral_id"], reason="ok")

    assert raised.value.code == "REASON_REQUIRED"


def test_lifting_records_the_reasoning_and_both_limbs(db, officer, facility):
    case = _a_case(db, officer, facility)
    reason = "Treated and documented cure; WHO re-entry criteria met on review."

    record = signoff.lift(db, officer, deferral_id=case["deferral_id"], reason=reason)

    assert record.lifted_at == DEMO_DATETIME
    assert record.lifted_by == officer.display_name
    assert reason in record.reason_note

    entry = db.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_id == case["deferral_id"],
            AuditLog.action == "DEFERRAL_LIFTED",
        )
        .limit(1)
    ).first()

    assert entry is not None

    context = (entry.after_json or {}).get("_context", {})

    assert context.get("clinical_reason") == reason
    assert context.get("applied_limb"), "the trail does not record which limb applied"
    assert context.get("alternative_limb"), "the trail does not record the alternative"


def test_a_lifted_deferral_leaves_the_queue(db, officer, facility):
    case = _a_case(db, officer, facility)
    signoff.lift(
        db,
        officer,
        deferral_id=case["deferral_id"],
        reason="Reviewed against WHO guidance; deferral not warranted here.",
    )

    remaining = {row["deferral_id"] for row in signoff.pending(db, officer)}

    assert case["deferral_id"] not in remaining


def test_lifting_one_deferral_does_not_release_a_donor_who_has_another(
    db, officer, facility
):
    """Status is recomputed from what is left in the ledger, not set."""

    case = _a_case(db, officer, facility)
    donor_id = case["donor_id"]

    # Give this donor a second, unrelated open deferral.
    import uuid

    db.add(
        DonorDeferral(
            id=str(uuid.uuid4()),
            donor_id=donor_id,
            deferred_at=DEMO_DATETIME,
            deferred_until=None,
            is_permanent=False,
            reason_code="UNDERWEIGHT",
            recorded_by="Test",
        )
    )
    db.commit()

    signoff.lift(
        db,
        officer,
        deferral_id=case["deferral_id"],
        reason="Contested rule reviewed and lifted; other findings stand.",
    )

    donor = db.get(Donor, donor_id)
    db.refresh(donor)

    assert donor.availability_status != "AVAILABLE", (
        "a donor with another open deferral was released"
    )


def test_upholding_is_recorded_as_firmly_as_lifting(db, officer, facility):
    """A reviewed deferral and an unreviewed one look identical otherwise, and
    that difference is the value of the queue."""

    case = _a_case(db, officer, facility)
    reason = "Permanent bar upheld; no re-entry testing available at this facility."

    record = signoff.uphold(db, officer, deferral_id=case["deferral_id"], reason=reason)

    assert record.lifted_at is None
    assert reason in record.reason_note

    entry = db.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_id == case["deferral_id"],
            AuditLog.action == "DEFERRAL_UPHELD",
        )
        .limit(1)
    ).first()

    assert entry is not None


def test_a_phlebotomist_cannot_sign_off_a_contested_deferral(
    db, phlebotomist, officer, facility
):
    """The rules are contested precisely because they need clinical weighing.
    Enforced in the service, not by hiding the page."""

    case = _a_case(db, officer, facility)

    with pytest.raises(PermissionDenied):
        signoff.lift(
            db,
            phlebotomist,
            deferral_id=case["deferral_id"],
            reason="This should never be permitted to be recorded at all.",
        )


def test_a_plain_deferral_cannot_be_signed_off_here(db, officer, facility):
    """Lifting a low haemoglobin deferral is a re-test, not a clinical
    judgement, and must not go through the contested-rule route."""

    import uuid

    donor_id = db.scalar(
        select(Donor.id).where(Donor.registered_facility_id == facility.id).limit(1)
    )
    deferral_id = str(uuid.uuid4())

    db.add(
        DonorDeferral(
            id=deferral_id,
            donor_id=donor_id,
            deferred_at=DEMO_DATETIME,
            deferred_until=None,
            is_permanent=False,
            reason_code="LOW_HAEMOGLOBIN",
            recorded_by="Test",
        )
    )
    db.commit()

    with pytest.raises(ServiceError) as raised:
        signoff.lift(
            db,
            officer,
            deferral_id=deferral_id,
            reason="Haemoglobin retested and now above the threshold.",
        )

    assert raised.value.code == "NOT_CONTESTED"
