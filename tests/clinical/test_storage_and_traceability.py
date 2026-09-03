"""Storage invariants, the discard action, and both directions of the trace.

The storage tests here are stated as properties of the shelf rather than of the
generator, because they have to hold however the data got there. "No untested
unit sits on the issuable shelf" is true of a real blood bank on a Tuesday
afternoon, not just of a fresh `python -m datagen.storage`.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from db.models import (
    AuditLog,
    BloodUnit,
    Component,
    DonationTest,
    Donor,
    Facility,
    StorageLocation,
    TemperatureLog,
)
from services import discard as discard_service
from services import traceability
from services.audit import Actor, PermissionDenied, ServiceError

# Which store each component may live in. A platelet held at 4°C loses function
# and red cells frozen without cryoprotectant haemolyse, so this is a clinical
# constraint, not a filing convention.
ALLOWED_STORE = {
    "PRBC": {"BLOOD_BANK_FRIDGE", "QUARANTINE_FRIDGE"},
    "WB": {"BLOOD_BANK_FRIDGE", "QUARANTINE_FRIDGE"},
    "FFP": {"PLASMA_FREEZER", "QUARANTINE_FREEZER"},
    "CRYO": {"PLASMA_FREEZER", "QUARANTINE_FREEZER"},
    "PLT_RD": {"PLATELET_AGITATOR"},
    "PLT_APH": {"PLATELET_AGITATOR"},
}


# --------------------------------------------------------------- storage --


def test_no_quarantined_unit_sits_on_an_issuable_shelf(session):
    """Untested stock is stored physically apart.

    That separation is the entire reason a quarantine fridge exists: so an
    untested bag cannot be picked off the issuable shelf by somebody in a hurry.
    A status flag alone does not stop a pair of hands.

    Platelets are the one documented exception. They live five days, so
    quarantine is transient, and a second incubator at every facility is not a
    realistic ask — an untested platelet stays in the shared agitator and relies
    on its status. Red cells and plasma have quarantine storage at their own
    temperature and must be in it.
    """

    offenders = session.execute(
        select(func.count())
        .select_from(BloodUnit)
        .join(StorageLocation, StorageLocation.id == BloodUnit.storage_location_id)
        .where(
            BloodUnit.status == "QUARANTINE",
            StorageLocation.is_quarantine.is_(False),
            StorageLocation.has_agitator.is_(False),
        )
    ).scalar()

    assert offenders == 0, (
        f"{offenders} quarantined units are sitting in an issuable store, where "
        f"somebody can pick them up."
    )


def test_every_unit_is_in_a_store_that_can_hold_it(session):
    """A platelet in a blood bank fridge is a destroyed platelet."""

    rows = session.execute(
        select(
            Component.code,
            StorageLocation.location_type,
            func.count().label("units"),
        )
        .select_from(BloodUnit)
        .join(Component, Component.id == BloodUnit.component_id)
        .join(StorageLocation, StorageLocation.id == BloodUnit.storage_location_id)
        .group_by(Component.code, StorageLocation.location_type)
    ).all()

    wrong = [
        (row.code, row.location_type, row.units)
        for row in rows
        if row.location_type not in ALLOWED_STORE.get(row.code, set())
    ]

    assert not wrong, f"components stored at the wrong temperature: {wrong}"


def test_a_stores_own_last_reading_agrees_with_its_log():
    """The denormalised field must not drift from the readings behind it.

    `storage_location.last_temp_c` exists so a page does not have to scan the
    log. The moment it disagrees with the log, every page using it is lying.
    """

    from db.session import SessionLocal

    db = SessionLocal()

    try:
        drifted = db.execute(
            text(
                """
                SELECT COUNT(*) FROM storage_location s
                WHERE s.last_temp_at IS NOT NULL AND NOT EXISTS (
                  SELECT 1 FROM temperature_log t
                  WHERE t.storage_location_id = s.id
                    AND t.recorded_at = s.last_temp_at
                    AND t.temperature_c = s.last_temp_c)
                """
            )
        ).scalar()
    finally:
        db.close()

    assert drifted == 0, f"{drifted} stores carry a last reading their log denies"


def test_a_flagged_unit_was_actually_in_a_store_that_breached():
    """The cold-chain flag blocks transfers, so it has to be earned.

    It was previously set on 118 units at random — 87 of them in stores that
    never went out of range — while 8,336 units that genuinely sat through an
    excursion read zero. A flag that means nothing is worse than no flag,
    because it gets acted on.
    """

    from db.session import SessionLocal

    db = SessionLocal()

    try:
        unearned = db.execute(
            text(
                """
                SELECT COUNT(*) FROM blood_unit b
                WHERE b.cold_chain_breach_count > 0
                  -- Only stock still on a shelf. A discarded or issued unit
                  -- keeps the flag it earned but loses its store pointer, and
                  -- erasing the flag to keep this query tidy would delete the
                  -- reason the unit was thrown away.
                  AND b.storage_location_id IS NOT NULL
                  AND NOT EXISTS (
                  SELECT 1 FROM temperature_log t
                  WHERE t.storage_location_id = b.storage_location_id
                    AND t.is_out_of_range = 1
                    AND t.recorded_at >= b.collected_at)
                """
            )
        ).scalar()
    finally:
        db.close()

    assert unearned == 0, (
        f"{unearned} units carry a cold-chain flag with no excursion behind it, "
        f"and cannot be transferred because of it."
    )


def test_readings_outside_the_target_band_are_marked_out_of_range():
    """The flag on a reading must follow from the reading and the band."""

    from db.session import SessionLocal

    db = SessionLocal()

    try:
        mismatched = db.execute(
            text(
                """
                SELECT COUNT(*) FROM temperature_log t
                JOIN storage_location s ON s.id = t.storage_location_id
                WHERE t.is_out_of_range <> (
                  t.temperature_c < s.target_temp_min_c
                  OR t.temperature_c > s.target_temp_max_c)
                """
            )
        ).scalar()
    finally:
        db.close()

    assert mismatched == 0, f"{mismatched} readings are flagged against their band"


# ---------------------------------------------------------------- discard --


@pytest.fixture
def scratch_session(scratch_database):
    maker = sessionmaker(bind=scratch_database)
    db = maker()

    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture
def officer(scratch_session):
    facility = scratch_session.scalars(select(Facility).limit(1)).first()

    return Actor(
        user_id=str(uuid.uuid4()),
        display_name="Test Officer",
        role="BLOOD_BANK_OFFICER",
        facility_id=facility.id,
    )


def _available_unit(db, facility_id, status="AVAILABLE"):
    return db.scalars(
        select(BloodUnit)
        .where(BloodUnit.facility_id == facility_id, BloodUnit.status == status)
        .limit(1)
    ).first()


def test_a_phlebotomist_cannot_discard(scratch_session, officer):
    unit = _available_unit(scratch_session, officer.facility_id)
    phlebotomist = Actor(
        user_id=str(uuid.uuid4()),
        display_name="Test Phlebotomist",
        role="PHLEBOTOMIST",
        facility_id=officer.facility_id,
    )

    with pytest.raises(PermissionDenied):
        discard_service.discard(
            scratch_session, phlebotomist, unit_id=unit.id, reason="HAEMOLYSIS"
        )


def test_a_reason_off_the_list_is_refused(scratch_session, officer):
    """Wastage is only reducible if its cause is known."""

    unit = _available_unit(scratch_session, officer.facility_id)

    with pytest.raises(ServiceError) as raised:
        discard_service.discard(
            scratch_session, officer, unit_id=unit.id, reason="BECAUSE_I_SAID_SO"
        )

    assert raised.value.code == "UNKNOWN_REASON"


def test_other_needs_a_note(scratch_session, officer):
    unit = _available_unit(scratch_session, officer.facility_id)

    with pytest.raises(ServiceError) as raised:
        discard_service.discard(
            scratch_session, officer, unit_id=unit.id, reason="OTHER", note="oops"
        )

    assert raised.value.code == "NOTE_REQUIRED"


def test_a_unit_committed_to_a_patient_cannot_be_discarded(
    scratch_session, officer
):
    """Throwing away a reserved unit leaves a patient holding a claim on a bag
    that no longer exists, and the ward finds out at the bedside."""

    unit = _available_unit(scratch_session, officer.facility_id, status="RESERVED")

    if unit is None:
        pytest.skip("no reserved unit at this facility to test against")

    with pytest.raises(ServiceError) as raised:
        discard_service.discard(
            scratch_session, officer, unit_id=unit.id, reason="HAEMOLYSIS"
        )

    assert raised.value.code == "COMMITTED_TO_PATIENT"


def test_a_unit_at_another_facility_is_not_visible(scratch_session, officer):
    other = scratch_session.scalars(
        select(BloodUnit)
        .where(
            BloodUnit.facility_id != officer.facility_id,
            BloodUnit.status == "AVAILABLE",
        )
        .limit(1)
    ).first()

    with pytest.raises(ServiceError) as raised:
        discard_service.discard(
            scratch_session, officer, unit_id=other.id, reason="HAEMOLYSIS"
        )

    assert raised.value.code == "UNIT_NOT_FOUND"


def test_discarding_takes_the_unit_off_the_shelf_and_records_the_cost(
    scratch_session, officer
):
    """The audit entry has to carry what the unit was worth when it was lost.

    A unit discarded with thirty days left is a different failure from one
    discarded with two hours left, and afterwards only this row can tell them
    apart.
    """

    unit = scratch_session.scalars(
        select(BloodUnit)
        .where(
            BloodUnit.facility_id == officer.facility_id,
            BloodUnit.status == "AVAILABLE",
            BloodUnit.storage_location_id.is_not(None),
        )
        .limit(1)
    ).first()

    unit_id = unit.id
    assert unit.storage_location_id is not None

    discard_service.discard(
        scratch_session,
        officer,
        unit_id=unit_id,
        reason="BROKEN_COLD_CHAIN",
        note="Fridge door left open overnight.",
    )

    refreshed = scratch_session.get(BloodUnit, unit_id)

    assert refreshed.status == "DISCARDED"
    assert refreshed.discard_reason == "BROKEN_COLD_CHAIN"
    # It leaves the shelf physically as well as on paper, or it shows up in that
    # fridge's contents during an excursion investigation as a false lead.
    assert refreshed.storage_location_id is None

    entry = scratch_session.scalars(
        select(AuditLog)
        .where(AuditLog.entity_id == unit_id, AuditLog.action == "UNIT_DISCARDED")
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    ).first()

    assert entry is not None, "a discard with no audit entry is not a discard"

    context = (entry.after_json or {}).get("_context", {})

    assert context.get("reason") == "BROKEN_COLD_CHAIN"
    assert context.get("preventable") is True
    assert context.get("days_of_shelf_life_lost") is not None
    assert context.get("was_stored_in")


def test_a_unit_cannot_be_discarded_twice(scratch_session, officer):
    """Or the wastage figures count the same bag twice."""

    unit = _available_unit(scratch_session, officer.facility_id)
    unit_id = unit.id

    discard_service.discard(
        scratch_session, officer, unit_id=unit_id, reason="CLOTS"
    )

    with pytest.raises(ServiceError) as raised:
        discard_service.discard(
            scratch_session, officer, unit_id=unit_id, reason="HAEMOLYSIS"
        )

    assert raised.value.code == "ALREADY_FINISHED"


def test_separation_is_not_counted_as_wastage(scratch_session, officer):
    """A separated bag became its components. The blood is still on the shelf
    under different numbers, so counting it as wastage double-counts it."""

    summary = discard_service.wastage_summary(scratch_session, officer.facility_id)
    reasons = {row["reason"] for row in summary["breakdown"]}

    assert "SEPARATED_INTO_COMPONENTS" not in reasons


def test_every_wastage_row_has_a_readable_label(scratch_session, officer):
    """Including the reasons the system applies rather than an operator picking
    them — a reactive result is not on the discard form but is real wastage."""

    summary = discard_service.wastage_summary(scratch_session, officer.facility_id)

    for row in summary["breakdown"]:
        assert row["label"], f"{row['reason']} renders with no label"
        assert row["label"] != row["reason"], (
            f"{row['reason']} falls through to its raw code"
        )


# ----------------------------------------------------------- traceability --


def test_a_unit_traces_back_through_its_whole_chain(scratch_session, officer):
    unit = scratch_session.scalars(
        select(BloodUnit)
        .where(
            BloodUnit.facility_id == officer.facility_id,
            BloodUnit.donation_id.is_not(None),
            BloodUnit.status == "AVAILABLE",
        )
        .limit(1)
    ).first()

    trace = traceability.trace_unit(scratch_session, officer, unit_id=unit.id)

    assert trace is not None
    assert trace["complete"] is True
    assert trace["donor"] is not None

    stages = [step.stage for step in trace["steps"]]

    assert "COLLECTED" in stages
    assert "TESTED" in stages

    # Oldest first. A chain of custody out of order is not a chain.
    stamps = [step.happened_at for step in trace["steps"] if step.happened_at]
    assert stamps == sorted(stamps)


def test_a_trace_carries_the_kit_lot(scratch_session, officer):
    """The field a recall actually asks for. A reactive lot has to be findable
    across every donation it touched."""

    unit = scratch_session.scalars(
        select(BloodUnit)
        .join(DonationTest, DonationTest.donation_id == BloodUnit.donation_id)
        .where(
            BloodUnit.facility_id == officer.facility_id,
            BloodUnit.donation_id.is_not(None),
            DonationTest.kit_lot.is_not(None),
        )
        .limit(1)
    ).first()

    trace = traceability.trace_unit(scratch_session, officer, unit_id=unit.id)
    tested = next(
        (step for step in trace["steps"] if step.stage == "TESTED"), None
    )

    assert tested is not None
    assert all(row["kit_lot"] for row in tested.detail["results"])


def test_a_unit_at_another_facility_does_not_trace(scratch_session, officer):
    """The network layer exchanges shared aggregates, never a donor identity."""

    other = scratch_session.scalars(
        select(BloodUnit)
        .where(BloodUnit.facility_id != officer.facility_id)
        .limit(1)
    ).first()

    assert traceability.trace_unit(scratch_session, officer, unit_id=other.id) is None


def test_the_forward_trace_separates_recoverable_from_gone(
    scratch_session, officer
):
    """The recall query. What can be pulled off a shelf, and what is already in
    a patient — those need different actions from different people."""

    donor_id = scratch_session.execute(
        text(
            """
            SELECT d.donor_id FROM donation d
            JOIN donor dn ON dn.id = d.donor_id
            WHERE dn.registered_facility_id = :facility
            LIMIT 1
            """
        ),
        {"facility": officer.facility_id},
    ).scalar()

    trace = traceability.trace_donor(scratch_session, officer, donor_id=donor_id)

    assert trace is not None

    summary = trace["summary"]

    assert summary["total"] == (
        summary["recoverable"] + summary["transfused"] + summary["other"]
    ), "every unit must land in exactly one bucket"

    assert all(u.status in traceability.RECOVERABLE for u in trace["recoverable"])
    assert all(u.status in traceability.GONE for u in trace["transfused"])


def test_the_forward_trace_states_what_it_cannot_see(scratch_session, officer):
    """A donor whose card reads eleven donations and whose ledger holds one is
    not a data error — the rest predate go-live. During a recall that gap is the
    answer, so it has to be reported rather than silently dropped."""

    donor = scratch_session.scalars(
        select(Donor)
        .where(
            Donor.registered_facility_id == officer.facility_id,
            Donor.total_donations > 0,
        )
        .limit(1)
    ).first()

    if donor is None:
        pytest.skip("no donor with a lifetime count at this facility")

    trace = traceability.trace_donor(scratch_session, officer, donor_id=donor.id)
    summary = trace["summary"]

    assert summary["before_go_live"] == max(
        0, (donor.total_donations or 0) - len(trace["donations"])
    )


def test_an_excursion_names_the_units_that_were_in_the_store(scratch_session):
    """The query an excursion exists to answer. A breach nobody can tie to stock
    is a number, not a finding."""

    row = scratch_session.execute(
        text(
            """
            SELECT storage_location_id AS store,
                   MIN(recorded_at) AS started,
                   MAX(recorded_at) AS ended
            FROM temperature_log
            WHERE is_out_of_range = 1
            GROUP BY storage_location_id
            LIMIT 1
            """
        )
    ).first()

    if row is None:
        pytest.skip("no excursion in the demo data")

    started = scratch_session.scalars(
        select(TemperatureLog.recorded_at)
        .where(
            TemperatureLog.storage_location_id == row.store,
            TemperatureLog.is_out_of_range.is_(True),
        )
        .order_by(TemperatureLog.recorded_at)
        .limit(1)
    ).first()

    ended = scratch_session.scalars(
        select(TemperatureLog.recorded_at)
        .where(
            TemperatureLog.storage_location_id == row.store,
            TemperatureLog.is_out_of_range.is_(True),
        )
        .order_by(TemperatureLog.recorded_at.desc())
        .limit(1)
    ).first()

    exposed = traceability.units_in_store_during(
        scratch_session, location_id=row.store, start=started, end=ended
    )

    # Anything returned must genuinely have been inside, and still there.
    for unit in exposed:
        assert unit.storage_location_id == row.store
        assert unit.collected_at <= ended
