"""Collection sessions: the bench, and the camps that run on top of it.

A session is the container everything else hangs from — a screening belongs to
one, a donation belongs to the screening, a unit belongs to the donation. Get
this wrong and the traceability chain has no root.

Two kinds, and the difference is not cosmetic. An in-house bench runs most days
at the facility itself and its identity is the facility. An outreach camp happens
somewhere else, is organised by somebody else, has a target somebody committed
to, and closes at the end of the day whether or not the target was met. A camp
that collected 40 units against a target of 100 is the single most useful record
a recruitment team has, and it only exists if the target was captured when the
session opened rather than reconstructed afterwards.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Permission
from core.clock import DEMO_DATETIME
from db.models import (
    Donation,
    DonationSession,
    DonorScreening,
    Facility,
)
from services.audit import Actor, ServiceError, audited, require, snapshot

SESSION_FIELDS = (
    "session_code",
    "facility_id",
    "session_type",
    "name",
    "venue",
    "district",
    "scheduled_date",
    "opened_at",
    "closed_at",
    "target_units",
    "status",
    "organiser",
    "contact_phone",
)

SESSION_TYPES = {
    "IN_HOUSE": "In-house bench",
    "OUTREACH_CAMP": "Outreach camp",
    "MOBILE_UNIT": "Mobile unit",
}

OPEN_STATUSES = ("PLANNED", "OPEN")


def open_session(
    db: Session,
    actor: Actor,
    *,
    session_type: str = "IN_HOUSE",
    name: str | None = None,
    venue: str | None = None,
    target_units: int = 0,
    organiser: str | None = None,
    contact_phone: str | None = None,
    scheduled_date: date | None = None,
    notes: str | None = None,
) -> DonationSession:
    """Open a session to collect against."""

    require(actor, Permission.COLLECT_DONATION, "open a collection session")

    if not actor.facility_id:
        raise ServiceError(
            "NO_FACILITY",
            "Select a facility before opening a session.",
        )

    if session_type not in SESSION_TYPES:
        raise ServiceError("BAD_SESSION_TYPE", "That is not a session type.", field="session_type")

    facility = db.get(Facility, actor.facility_id)

    if facility is None:
        raise ServiceError("FACILITY_NOT_FOUND", "That facility does not exist.")

    when = scheduled_date or DEMO_DATETIME.date()

    if session_type == "OUTREACH_CAMP" and not venue:
        # A camp with no venue cannot be found again, and its geography is the
        # reason it exists as a separate record from the bench.
        raise ServiceError(
            "VENUE_REQUIRED",
            "An outreach camp needs a venue.",
            field="venue",
        )

    record = DonationSession(
        id=str(uuid.uuid4()),
        session_code=_next_code(db, facility, when, session_type),
        facility_id=facility.id,
        organization_id=facility.organization_id,
        session_type=session_type,
        name=name or _default_name(facility, session_type, venue),
        venue=venue or facility.name_en,
        district=facility.district,
        latitude=facility.latitude,
        longitude=facility.longitude,
        scheduled_date=when,
        opened_at=DEMO_DATETIME,
        target_units=int(target_units or 0),
        status="OPEN",
        organiser=organiser,
        contact_phone=contact_phone,
        notes=notes,
        created_by=actor.display_name,
        created_at=DEMO_DATETIME,
    )

    with audited(db, actor, "SESSION_OPENED", "donation_session") as entry:
        db.add(record)
        db.flush()
        entry.on(record, after=snapshot(record, SESSION_FIELDS))

    return record


def close_session(
    db: Session, actor: Actor, *, session_id: str, notes: str | None = None
) -> DonationSession:
    """Close a session and freeze what it achieved.

    Closing does not delete anything or stop the records existing; it stops new
    screenings attaching, which is what makes the collected-against-target figure
    mean something afterwards.
    """

    require(actor, Permission.COLLECT_DONATION, "close a collection session")

    record = _own_session(db, actor, session_id)

    if record.status == "CLOSED":
        raise ServiceError("ALREADY_CLOSED", "That session is already closed.")

    before = snapshot(record, SESSION_FIELDS)
    collected = count_donations(db, session_id)

    with audited(db, actor, "SESSION_CLOSED", "donation_session", session_id) as entry:
        record.status = "CLOSED"
        record.closed_at = DEMO_DATETIME

        if notes:
            record.notes = f"{record.notes}\n{notes}" if record.notes else notes

        db.flush()

        entry.on(record, before=before, after=snapshot(record, SESSION_FIELDS))
        entry.note(
            units_collected=collected,
            target_units=record.target_units,
            # Recorded at close rather than derived later, because the target is
            # what somebody committed to on the day.
            shortfall=max(0, (record.target_units or 0) - collected),
        )

    return record


def _own_session(db: Session, actor: Actor, session_id: str) -> DonationSession:
    """A session at the actor's own facility, or nothing.

    The lookup and the tenancy check are one query. Splitting them into "find,
    then check" is how a cross-tenant read gets shipped.
    """

    record = db.scalars(
        select(DonationSession).where(
            DonationSession.id == session_id,
            DonationSession.facility_id == actor.facility_id,
        )
    ).first()

    if record is None:
        raise ServiceError("SESSION_NOT_FOUND", "That session does not exist here.")

    return record


def current_session(db: Session, actor: Actor) -> DonationSession | None:
    """The open session a screening should attach to, if there is one."""

    return db.scalars(
        select(DonationSession)
        .where(
            DonationSession.facility_id == actor.facility_id,
            DonationSession.status.in_(OPEN_STATUSES),
        )
        .order_by(DonationSession.opened_at.desc())
        .limit(1)
    ).first()


def ensure_session(db: Session, actor: Actor) -> DonationSession:
    """The open session, opening today's bench if none is running.

    A screening must belong to a session. Rather than refuse the first screening
    of the day, the bench opens itself — which is what happens in practice, since
    nobody declares the bench open before drawing the first bag.
    """

    existing = current_session(db, actor)

    if existing is not None:
        return existing

    return open_session(db, actor, session_type="IN_HOUSE")


def count_donations(db: Session, session_id: str) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(Donation)
            .where(Donation.session_id == session_id)
        )
        or 0
    )


def session_summary(db: Session, session_id: str) -> dict:
    """What a session achieved, counted from the records rather than a tally.

    Screenings and donations are counted separately because the gap between them
    is the deferral rate, which is the number a camp organiser actually wants.
    """

    screened = (
        db.scalar(
            select(func.count())
            .select_from(DonorScreening)
            .where(
                DonorScreening.session_id == session_id,
                # A draft is somebody mid-way through, not a screening.
                DonorScreening.outcome != "DRAFT",
            )
        )
        or 0
    )
    deferred = (
        db.scalar(
            select(func.count())
            .select_from(DonorScreening)
            .where(
                DonorScreening.session_id == session_id,
                DonorScreening.outcome == "DEFERRED",
            )
        )
        or 0
    )
    collected = count_donations(db, session_id)

    return {
        "screened": screened,
        "deferred": deferred,
        "accepted": screened - deferred,
        "collected": collected,
        "deferral_rate": (deferred / screened) if screened else 0.0,
        # Accepted but not collected: a donor who passed screening and then did
        # not give. Worth surfacing — it is usually a failed venepuncture or a
        # donor who changed their mind, and neither shows up anywhere else.
        "did_not_donate": max(0, (screened - deferred) - collected),
    }


def _default_name(facility: Facility, session_type: str, venue: str | None) -> str:
    if session_type == "IN_HOUSE":
        return f"{facility.name_en} donor bench"

    return f"{venue} blood drive" if venue else f"{facility.name_en} outreach"


def _next_code(
    db: Session, facility: Facility, when: date, session_type: str
) -> str:
    """A readable code, unique per facility and day.

    Sessions are referred to out loud and written on paper forms at a camp, so
    the code has to be sayable — not a UUID.
    """

    suffix = "BENCH" if session_type == "IN_HOUSE" else "CAMP"
    base = f"{facility.code}-{when:%y%m%d}-{suffix}"

    taken = set(
        db.scalars(
            select(DonationSession.session_code).where(
                DonationSession.session_code.like(f"{base}%")
            )
        ).all()
    )

    if base not in taken:
        return base

    index = 2

    while f"{base}-{index}" in taken:
        index += 1

    return f"{base}-{index}"
