"""Lab testing and release: the control point where a bag becomes issuable.

Everything before this produces quarantined units. Everything after it assumes
the unit is safe. That makes release the single most consequential write in the
system, and it is guarded by two rules the service will not let a caller past:

1. **Nobody releases their own work.** The technologist who recorded a result
   cannot be the one who verifies it. Not a UI convention — `release` refuses.

2. **A reactive result has all its consequences at once.** Unit discarded,
   donation quarantined, donor deferred, confirmatory test raised, in one
   transaction. There is no window in which a reactive unit is still issuable
   because somebody had not clicked the next button yet.

A reactive screen is not a diagnosis. The unit goes immediately because the
supply cannot wait; the DONOR's status waits for a confirmatory assay, because
roughly half of screen-reactive donors are not infected and permanently labelling
them would be a real harm to a real person.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Integer, cast, func, or_, select
from sqlalchemy.orm import Session

from app.auth import Permission
from core import config
from core.clock import DEMO_DATETIME
from db.models import (
    BloodUnit,
    Donation,
    DonationTest,
    Donor,
    DonorDeferral,
    Facility,
    LabRun,
)
from services.audit import Actor, ServiceError, audited, require, snapshot

RUN_FIELDS = (
    "run_code",
    "facility_id",
    "test_code",
    "method",
    "kit_lot",
    "kit_expiry",
    "equipment",
    "status",
    "controls_valid",
    "opened_at",
    "opened_by",
    "closed_at",
    "closed_by",
)

RESULT_FIELDS = (
    "donation_id",
    "test_code",
    "test_group",
    "result",
    "is_reactive",
    "tested_at",
    "tested_by",
    "verified_at",
    "verified_by",
)

REACTIVE = "REACTIVE"
NON_REACTIVE = "NON_REACTIVE"


def required_tests() -> list[str]:
    """The panel every donation must clear before it can be released."""

    tests = list(config.get("tti_panel.required_tests") or [])

    if not config.get("tti_panel.malaria_screening_enabled"):
        tests = [code for code in tests if code != "MALARIA"]

    return tests


def confirmatory_method(marker: str) -> str:
    methods = dict(config.get("tti_panel.confirmatory.method") or {})

    return methods.get(marker, "Confirmatory assay")


def unconfirmed_deferral_days() -> int:
    return int(config.get("tti_panel.confirmatory.unconfirmed_deferral_days") or 180)


# ------------------------------------------------------------------ worklist


@dataclass
class PendingDonation:
    """A donation waiting on the lab, with what it still needs."""

    donation_id: str
    din: str
    collected_at: object
    donor_code: str | None
    outstanding: list[str]
    hours_waiting: float

    @property
    def is_complete(self) -> bool:
        return not self.outstanding


def pending(db: Session, actor: Actor, *, limit: int = 200) -> list[PendingDonation]:
    """Donations at this facility that are not yet fully tested.

    Ordered oldest first: a bag sitting untested is shelf life spent on nothing,
    and platelets have five days of it.
    """

    panel = set(required_tests())

    rows = db.execute(
        select(
            Donation.id,
            Donation.din,
            Donation.collected_at,
            Donor.donor_code,
        )
        .join(Donor, Donor.id == Donation.donor_id)
        .where(
            Donation.facility_id == actor.facility_id,
            Donation.status.in_(("COLLECTED", "TESTING")),
        )
        .order_by(Donation.collected_at)
        .limit(limit)
    ).all()

    if not rows:
        return []

    done: dict[str, set[str]] = {}

    for donation_id, test_code in db.execute(
        select(DonationTest.donation_id, DonationTest.test_code).where(
            DonationTest.donation_id.in_([row.id for row in rows]),
            DonationTest.test_group == "TTI",
        )
    ).all():
        done.setdefault(donation_id, set()).add(test_code)

    result = []

    for row in rows:
        outstanding = sorted(panel - done.get(row.id, set()))
        waiting = (DEMO_DATETIME - row.collected_at).total_seconds() / 3600.0

        result.append(
            PendingDonation(
                donation_id=row.id,
                din=row.din,
                collected_at=row.collected_at,
                donor_code=row.donor_code,
                outstanding=outstanding,
                hours_waiting=round(waiting, 1),
            )
        )

    return result


def pending_count(db: Session, facility_id: str | None) -> int:
    if not facility_id:
        return 0

    return (
        db.scalar(
            select(func.count())
            .select_from(Donation)
            .where(
                Donation.facility_id == facility_id,
                Donation.status.in_(("COLLECTED", "TESTING")),
            )
        )
        or 0
    )


def awaiting_release(db: Session, actor: Actor, *, limit: int = 200) -> list[dict]:
    """Donations whose panel is complete but which nobody has released.

    Separate from `pending` because it is a different person's job, and because
    a bag stuck here is one signature away from being usable.
    """

    panel = required_tests()

    rows = db.execute(
        select(
            Donation.id,
            Donation.din,
            Donation.collected_at,
            Donation.status,
            Donor.donor_code,
            func.count(DonationTest.id).label("results"),
            func.max(DonationTest.tested_by).label("tested_by"),
            func.sum(cast(DonationTest.is_reactive, Integer)).label("reactive"),
        )
        .join(Donor, Donor.id == Donation.donor_id)
        .join(DonationTest, DonationTest.donation_id == Donation.id)
        .where(
            Donation.facility_id == actor.facility_id,
            Donation.status.in_(("COLLECTED", "TESTING")),
            DonationTest.test_group == "TTI",
        )
        .group_by(Donation.id, Donation.din, Donation.collected_at, Donation.status, Donor.donor_code)
        .having(func.count(DonationTest.id) >= len(panel))
        .order_by(Donation.collected_at)
        .limit(limit)
    ).all()

    return [
        {
            "donation_id": row.id,
            "din": row.din,
            "collected_at": row.collected_at,
            "donor_code": row.donor_code,
            "tested_by": row.tested_by,
            "reactive": bool(row.reactive),
            # Whether THIS actor may release it. Shown rather than hidden, so a
            # technologist can see the bag is ready and that somebody else has
            # to sign it — which is the point of the control.
            "releasable_by_actor": row.tested_by != actor.display_name,
        }
        for row in rows
    ]


# ----------------------------------------------------------------- lab runs


def open_run(
    db: Session,
    actor: Actor,
    *,
    test_code: str,
    method: str | None = None,
    kit_lot: str | None = None,
    kit_expiry: date | None = None,
    equipment: str | None = None,
) -> LabRun:
    """Start a plate."""

    require(actor, Permission.PERFORM_TEST, "run laboratory tests")

    if test_code not in required_tests():
        raise ServiceError(
            "UNKNOWN_TEST",
            f"{test_code} is not in the configured screening panel.",
            field="test_code",
        )

    if kit_expiry is not None and kit_expiry < DEMO_DATETIME.date():
        # An expired kit does not produce a result worth having, and a lab that
        # can record one will eventually record one.
        raise ServiceError(
            "KIT_EXPIRED",
            f"That kit lot expired on {kit_expiry:%d %b %Y}.",
            field="kit_expiry",
        )

    record = LabRun(
        id=str(uuid.uuid4()),
        run_code=_next_run_code(db, actor.facility_id, test_code),
        facility_id=actor.facility_id,
        test_code=test_code,
        test_group="TTI",
        method=method,
        kit_lot=kit_lot,
        kit_expiry=kit_expiry,
        equipment=equipment,
        status="OPEN",
        opened_at=DEMO_DATETIME,
        opened_by=actor.display_name,
    )

    with audited(db, actor, "LAB_RUN_OPENED", "lab_run") as entry:
        db.add(record)
        db.flush()
        entry.on(record, after=snapshot(record, RUN_FIELDS))

    return record


def record_results(
    db: Session,
    actor: Actor,
    *,
    run_id: str,
    results: dict[str, str],
) -> dict:
    """Record a plate's results.

    `results` maps donation id to REACTIVE or NON_REACTIVE. A reactive result
    takes effect immediately and completely — see `_handle_reactive`.
    """

    require(actor, Permission.PERFORM_TEST, "run laboratory tests")

    run = _own_run(db, actor, run_id)

    if run.status == "CLOSED":
        raise ServiceError("RUN_CLOSED", "That run has already been closed.")

    if run.controls_valid is False:
        raise ServiceError(
            "CONTROLS_FAILED",
            "This plate's controls failed, so its results cannot be interpreted.",
        )

    recorded = 0
    reactive_donations = []

    with audited(db, actor, "LAB_RESULTS_RECORDED", "lab_run", run_id) as entry:
        for donation_id, value in results.items():
            outcome = REACTIVE if str(value).upper() == REACTIVE else NON_REACTIVE

            donation = db.get(Donation, donation_id)

            if donation is None or donation.facility_id != actor.facility_id:
                continue

            existing = db.scalars(
                select(DonationTest).where(
                    DonationTest.donation_id == donation_id,
                    DonationTest.test_code == run.test_code,
                    DonationTest.test_group == "TTI",
                )
            ).first()

            if existing is not None:
                # A repeat needs its own run and its own record; silently
                # overwriting a result is how a reactive one disappears.
                continue

            db.add(
                DonationTest(
                    id=str(uuid.uuid4()),
                    donation_id=donation_id,
                    lab_run_id=run.id,
                    test_code=run.test_code,
                    test_group="TTI",
                    method=run.method,
                    kit_lot=run.kit_lot,
                    kit_expiry=run.kit_expiry,
                    equipment=run.equipment,
                    result=outcome,
                    is_reactive=outcome == REACTIVE,
                    tested_at=DEMO_DATETIME,
                    tested_by=actor.display_name,
                )
            )
            recorded += 1

            if donation.status == "COLLECTED":
                donation.status = "TESTING"

            if outcome == REACTIVE:
                _handle_reactive(db, actor, donation, run.test_code)
                reactive_donations.append(donation.din)

        run.status = "RESULTS_ENTERED"
        db.flush()

        entry.on(run, after=snapshot(run, RUN_FIELDS))
        entry.note(
            results_recorded=recorded,
            reactive=len(reactive_donations),
            reactive_dins=reactive_donations,
            kit_lot=run.kit_lot,
        )

    return {"recorded": recorded, "reactive": len(reactive_donations)}


def _handle_reactive(
    db: Session, actor: Actor, donation: Donation, marker: str
) -> None:
    """Everything a reactive result implies, at the moment it is recorded.

    The unit goes now because the supply cannot wait on a second opinion. The
    DONOR's status waits for confirmation, because a reactive screen is not a
    diagnosis and roughly half of those flagged are not infected — permanently
    labelling them would be a real harm to a real person.
    """

    donation.status = "QUARANTINED"
    donation.released_at = None
    donation.released_by = None

    units = db.scalars(
        select(BloodUnit).where(BloodUnit.donation_id == donation.id)
    ).all()

    for unit in units:
        if unit.status in ("TRANSFUSED", "ISSUED"):
            # Should be impossible — a quarantined unit is not issuable — but if
            # it ever happens the lookback must not be silently skipped.
            continue

        unit.status = "DISCARDED"
        unit.screening_status = "FAILED"
        unit.discard_reason = f"TTI_REACTIVE_{marker}"
        unit.discarded_at = DEMO_DATETIME

    # Raise the confirmatory test rather than leaving somebody to remember.
    db.add(
        DonationTest(
            id=str(uuid.uuid4()),
            donation_id=donation.id,
            test_code=marker,
            test_group="TTI_CONFIRMATORY",
            method=confirmatory_method(marker),
            result="PENDING",
            is_reactive=False,
            tested_at=DEMO_DATETIME,
            notes=(
                "Raised automatically on a reactive screen. The donor is "
                "deferred pending this result and no inference about infection "
                "status may be drawn until it returns."
            ),
        )
    )

    donor = db.get(Donor, donation.donor_id)

    if donor is None:
        return

    already = db.scalars(
        select(DonorDeferral).where(
            DonorDeferral.donor_id == donor.id,
            DonorDeferral.lifted_at.is_(None),
            DonorDeferral.reason_code.like("TTI_AWAITING%"),
        )
    ).first()

    if already is None:
        db.add(
            DonorDeferral(
                id=str(uuid.uuid4()),
                donor_id=donor.id,
                deferred_at=DEMO_DATETIME,
                deferred_until=None,
                is_permanent=False,
                reason_code=f"TTI_AWAITING_CONFIRMATION_{marker}",
                reason_note=(
                    "Screening reactive. Confirmatory testing is outstanding; "
                    "the donor is deferred until it returns."
                ),
                recorded_by=actor.display_name,
            )
        )

    donor.availability_status = "AWAITING_TTI_CONFIRMATION"


def record_controls(
    db: Session,
    actor: Actor,
    *,
    run_id: str,
    controls_valid: bool,
    note: str | None = None,
) -> LabRun:
    """Record whether the plate's controls behaved.

    A failed control invalidates every result on the plate at once — the samples
    have to be re-run. Recording that is what stops an uninterpretable plate
    being released as though it meant something.
    """

    require(actor, Permission.PERFORM_TEST, "run laboratory tests")

    run = _own_run(db, actor, run_id)
    before = snapshot(run, RUN_FIELDS)

    with audited(db, actor, "LAB_RUN_CONTROLS", "lab_run", run_id) as entry:
        run.controls_valid = bool(controls_valid)
        run.control_note = note

        if not controls_valid:
            run.status = "INVALIDATED"

        db.flush()
        entry.on(run, before=before, after=snapshot(run, RUN_FIELDS))
        entry.note(controls_valid=bool(controls_valid), note=note)

    return run


# ------------------------------------------------------------------ release


def release(
    db: Session, actor: Actor, *, donation_id: str, note: str | None = None
) -> Donation:
    """Release a donation's units into issuable stock.

    The control point. Everything before it produces quarantined units;
    everything after assumes the unit is safe.
    """

    require(actor, Permission.VERIFY_TEST_RELEASE, "release tested units")

    donation = db.scalars(
        select(Donation).where(
            Donation.id == donation_id,
            Donation.facility_id == actor.facility_id,
        )
    ).first()

    if donation is None:
        raise ServiceError("DONATION_NOT_FOUND", "That donation does not exist here.")

    if donation.status == "RELEASED":
        raise ServiceError("ALREADY_RELEASED", "That donation is already released.")

    if donation.status == "QUARANTINED":
        raise ServiceError(
            "QUARANTINED",
            "This donation is quarantined on a reactive result and must not be "
            "released.",
        )

    results = db.scalars(
        select(DonationTest).where(
            DonationTest.donation_id == donation_id,
            DonationTest.test_group == "TTI",
        )
    ).all()

    outstanding = sorted(set(required_tests()) - {row.test_code for row in results})

    if outstanding:
        raise ServiceError(
            "PANEL_INCOMPLETE",
            "Not every required test has a result yet: "
            + ", ".join(outstanding)
            + ".",
        )

    reactive = [row for row in results if row.is_reactive]

    if reactive:
        # Defence in depth. `_handle_reactive` should already have quarantined
        # this, but release is the last gate and it does not assume that.
        raise ServiceError(
            "REACTIVE_RESULT",
            "This donation has a reactive result and must not be released.",
        )

    # Two-person rule. Checked against every result, not just one, because a
    # panel run entirely by one person is exactly the case this prevents.
    testers = {row.tested_by for row in results if row.tested_by}

    if actor.display_name in testers:
        raise ServiceError(
            "SELF_RELEASE",
            "You recorded these results, so you cannot also release them. "
            "A second person has to verify.",
        )

    invalid_runs = db.scalars(
        select(LabRun.run_code).where(
            LabRun.id.in_([row.lab_run_id for row in results if row.lab_run_id]),
            LabRun.controls_valid.is_(False),
        )
    ).all()

    if invalid_runs:
        raise ServiceError(
            "CONTROLS_FAILED",
            "Results came from a plate whose controls failed ("
            + ", ".join(invalid_runs)
            + "). Those samples must be re-run.",
        )

    before = snapshot(donation, ("status", "released_at", "released_by"))

    with audited(db, actor, "DONATION_RELEASED", "donation", donation_id) as entry:
        donation.status = "RELEASED"
        donation.released_at = DEMO_DATETIME
        donation.released_by = actor.display_name

        for row in results:
            row.verified_at = DEMO_DATETIME
            row.verified_by = actor.display_name

        units = db.scalars(
            select(BloodUnit).where(BloodUnit.donation_id == donation_id)
        ).all()

        for unit in units:
            if unit.status == "QUARANTINE":
                unit.status = "AVAILABLE"
                unit.screening_status = "PASSED"

        db.flush()

        entry.on(
            donation,
            before=before,
            after=snapshot(donation, ("status", "released_at", "released_by")),
        )
        entry.note(
            units_released=len(units),
            unit_dins=[unit.din for unit in units],
            tested_by=sorted(testers),
            verified_by=actor.display_name,
            note=note,
        )

    return donation


def record_confirmation(
    db: Session,
    actor: Actor,
    *,
    donation_id: str,
    marker: str,
    confirmed: bool,
    note: str | None = None,
) -> DonationTest:
    """Record a confirmatory result, and settle the donor's status on it.

    Positive confirms the infection and defers the donor permanently. Negative
    does not clear them outright — something made the screen react — so they are
    deferred for a period and re-tested before reinstatement.
    """

    require(actor, Permission.VERIFY_TEST_RELEASE, "record confirmatory results")

    pending_test = db.scalars(
        select(DonationTest)
        .join(Donation, Donation.id == DonationTest.donation_id)
        .where(
            DonationTest.donation_id == donation_id,
            DonationTest.test_code == marker,
            DonationTest.test_group == "TTI_CONFIRMATORY",
            Donation.facility_id == actor.facility_id,
        )
    ).first()

    if pending_test is None:
        raise ServiceError(
            "NO_CONFIRMATORY_TEST",
            "No confirmatory test is outstanding for that marker.",
        )

    if pending_test.result != "PENDING":
        raise ServiceError(
            "ALREADY_CONFIRMED", "That confirmatory result is already recorded."
        )

    donation = db.get(Donation, donation_id)
    donor = db.get(Donor, donation.donor_id)
    before = snapshot(pending_test, RESULT_FIELDS)

    with audited(
        db, actor, "TTI_CONFIRMATION_RECORDED", "donation_test", pending_test.id
    ) as entry:
        pending_test.result = "POSITIVE" if confirmed else "NEGATIVE"
        pending_test.is_reactive = bool(confirmed)
        pending_test.tested_at = DEMO_DATETIME
        pending_test.tested_by = actor.display_name
        pending_test.notes = note or pending_test.notes

        _settle_donor(db, actor, donor, marker=marker, confirmed=confirmed)

        db.flush()
        entry.on(pending_test, before=before, after=snapshot(pending_test, RESULT_FIELDS))
        entry.note(marker=marker, confirmed=bool(confirmed), note=note)

    return pending_test


def _settle_donor(
    db: Session, actor: Actor, donor: Donor | None, *, marker: str, confirmed: bool
) -> None:
    if donor is None:
        return

    awaiting = db.scalars(
        select(DonorDeferral).where(
            DonorDeferral.donor_id == donor.id,
            DonorDeferral.lifted_at.is_(None),
            DonorDeferral.reason_code.like("TTI_AWAITING%"),
        )
    ).all()

    for row in awaiting:
        row.lifted_at = DEMO_DATETIME
        row.lifted_by = actor.display_name
        row.reason_note = (
            f"{row.reason_note}\n" if row.reason_note else ""
        ) + (
            "Superseded by a confirmatory result."
        )

    if confirmed:
        db.add(
            DonorDeferral(
                id=str(uuid.uuid4()),
                donor_id=donor.id,
                deferred_at=DEMO_DATETIME,
                deferred_until=None,
                is_permanent=True,
                reason_code=f"TTI_CONFIRMED_{marker}",
                reason_note=(
                    f"Confirmatory testing positive for {marker}. Donor to be "
                    "notified and referred for counselling and treatment."
                ),
                recorded_by=actor.display_name,
            )
        )
        donor.is_permanently_deferred = True
        donor.deferred_until = None
        donor.availability_status = "PERMANENTLY_DEFERRED"
        return

    until = (DEMO_DATETIME + timedelta(days=unconfirmed_deferral_days())).date()

    db.add(
        DonorDeferral(
            id=str(uuid.uuid4()),
            donor_id=donor.id,
            deferred_at=DEMO_DATETIME,
            deferred_until=until,
            is_permanent=False,
            reason_code=f"TTI_UNCONFIRMED_{marker}",
            reason_note=(
                "Screening reactive, confirmatory testing negative. Deferred "
                "pending a repeat test before reinstatement."
            ),
            recorded_by=actor.display_name,
        )
    )
    donor.is_permanently_deferred = False
    donor.deferred_until = until
    donor.availability_status = "TEMPORARILY_DEFERRED"


# ------------------------------------------------------------------ helpers


def _own_run(db: Session, actor: Actor, run_id: str) -> LabRun:
    run = db.scalars(
        select(LabRun).where(
            LabRun.id == run_id, LabRun.facility_id == actor.facility_id
        )
    ).first()

    if run is None:
        raise ServiceError("RUN_NOT_FOUND", "That run does not exist here.")

    return run


def _next_run_code(db: Session, facility_id: str, test_code: str) -> str:
    """A code somebody can say out loud and write on a plate."""

    facility = db.get(Facility, facility_id)
    prefix = facility.code if facility else "LAB"
    base = f"{prefix}-{DEMO_DATETIME:%y%m%d}-{test_code}"

    taken = set(
        db.scalars(select(LabRun.run_code).where(LabRun.run_code.like(f"{base}%"))).all()
    )

    if base not in taken:
        return base

    index = 2

    while f"{base}-{index}" in taken:
        index += 1

    return f"{base}-{index}"
