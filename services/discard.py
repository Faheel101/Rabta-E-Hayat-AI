"""Discarding a unit, and counting what it cost.

Wastage is the number a blood bank is judged on, and it is the only outcome in
the whole chain where a donor gave blood and nobody received it. So a discard is
never a status flag set quietly: it is an action, by a named person, against a
reason from a fixed list, in a transaction that carries its own audit entry.

Two rules that look like friction and are not.

**A unit already committed to a patient cannot be discarded from here.** It is
reserved or crossmatched against somebody who is waiting. Throwing it away
without releasing the reservation leaves a patient holding a claim on a bag that
no longer exists, and the ward finds out at the bedside.

**A reason is required and free text is not one.** "Discarded — see notes" across
four hundred units tells you nothing. The same four hundred under
BROKEN_COLD_CHAIN tells you to fix a fridge.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Permission
from core import config
from core.clock import DEMO_DATETIME
from db.models import BloodGroup, BloodUnit, Component, StorageLocation
from services.audit import Actor, ServiceError, audited, require, snapshot

UNIT_FIELDS = ("status", "discarded_at", "discard_reason", "storage_location_id")

# Committed to a patient. Releasing that claim is a separate, deliberate act.
COMMITTED = ("RESERVED", "CROSSMATCHED")

# Already gone. Discarding twice would double-count the wastage.
FINISHED = ("DISCARDED", "TRANSFUSED", "ISSUED", "SEPARATED", "EXPIRED")

MINIMUM_NOTE = 8

# Reasons the system applies rather than an operator choosing them. They are not
# on the discard form — nobody picks "reactive for HCV" off a dropdown, the panel
# decides it — but they are real wastage and belong in the total.
#
# Marker names are written as a clinician writes them, and kept distinct rather
# than collapsed into one "reactive" row: a rise in one marker says something
# about the donor population that a combined total would bury.
SYSTEM_REASONS = {
    "TTI_REACTIVE_HIV": "Reactive — HIV",
    "TTI_REACTIVE_HBSAG": "Reactive — HBsAg",
    "TTI_REACTIVE_HCV": "Reactive — HCV",
    "TTI_REACTIVE_SYPHILIS": "Reactive — Syphilis",
    "TTI_REACTIVE_MALARIA": "Reactive — Malaria",
    "SCREENING_FAILED": "Donor failed screening after collection",
}


def reasons() -> dict[str, dict]:
    """The reason list, from config so it is reviewable without reading Python."""

    return dict(config.get("storage.discard_reasons") or {})


def reason_choices() -> list[dict]:
    """Reasons in a shape a form can render."""

    return [
        {
            "code": code,
            "label": spec.get("label", code.replace("_", " ").capitalize()),
            "preventable": bool(spec.get("preventable", False)),
        }
        for code, spec in reasons().items()
    ]


def discard(
    db: Session,
    actor: Actor,
    *,
    unit_id: str,
    reason: str,
    note: str | None = None,
) -> BloodUnit:
    """Throw a unit away, on the record.

    The audit entry carries what the unit was worth at the moment it was lost —
    component, group, and how much shelf life remained. A unit discarded with
    thirty days left is a different failure from one discarded with two hours
    left, and after the fact only the audit entry can tell them apart.
    """

    require(actor, Permission.DISCARD_UNIT, "discard a unit")

    if reason not in reasons():
        raise ServiceError(
            "UNKNOWN_REASON",
            "Pick a discard reason from the list. A unit thrown away without a "
            "recorded cause is wastage nobody can reduce.",
        )

    unit = db.scalars(
        select(BloodUnit).where(
            BloodUnit.id == unit_id,
            BloodUnit.facility_id == actor.facility_id,
        )
    ).first()

    if unit is None:
        raise ServiceError("UNIT_NOT_FOUND", "That unit is not held here.")

    if unit.status in FINISHED:
        raise ServiceError(
            "ALREADY_FINISHED",
            f"This unit is already {unit.status.lower()} and cannot be discarded "
            f"again.",
        )

    if unit.status in COMMITTED:
        raise ServiceError(
            "COMMITTED_TO_PATIENT",
            "This unit is committed to a patient. Release the reservation first "
            "— discarding it here would leave a claim on a bag that no longer "
            "exists, and the ward would find out at the bedside.",
        )

    if reason == "OTHER" and len((note or "").strip()) < MINIMUM_NOTE:
        raise ServiceError(
            "NOTE_REQUIRED",
            "'Other' needs a note saying what actually happened, or it is the "
            "same as recording nothing.",
        )

    before = snapshot(unit, UNIT_FIELDS)

    # What this unit was worth when it was lost. Computed before the write,
    # because afterwards the shelf life is meaningless.
    remaining = None

    if unit.expires_at:
        remaining = round(
            (unit.expires_at - DEMO_DATETIME).total_seconds() / 86400.0, 1
        )

    component = db.get(Component, unit.component_id)
    group = db.get(BloodGroup, unit.blood_group_id) if unit.blood_group_id else None
    location = (
        db.get(StorageLocation, unit.storage_location_id)
        if unit.storage_location_id
        else None
    )

    with audited(db, actor, "UNIT_DISCARDED", "blood_unit") as entry:
        unit.status = "DISCARDED"
        unit.discarded_at = DEMO_DATETIME
        unit.discard_reason = reason
        # It leaves the shelf physically as well as on paper. A discarded unit
        # still pointing at a fridge shows up in that fridge's contents during
        # an excursion investigation, which is a false lead.
        unit.storage_location_id = None

        db.flush()

        after = snapshot(unit, UNIT_FIELDS)

        entry.on(unit, before=before, after=after)
        entry.note(
            din=unit.din,
            reason=reason,
            preventable=bool(reasons().get(reason, {}).get("preventable")),
            note=(note or "").strip() or None,
            component=component.code if component else None,
            blood_group=group.code if group else None,
            # The cost. Thirty days of shelf life thrown away is a different
            # failure from two hours, and only this row remembers which.
            days_of_shelf_life_lost=remaining,
            volume_ml=unit.volume_ml,
            was_stored_in=location.name if location else None,
        )

    return unit


def wastage_summary(db: Session, facility_id: str, *, days: int = 30) -> dict:
    """What has been thrown away lately, and how much of it was avoidable.

    Split by preventable, because the two halves have different answers. A run
    of haemolysed units is a collection technique problem; a run of expiries is
    an ordering problem; a run of cold chain breaches is a maintenance problem.
    """

    since = DEMO_DATETIME - timedelta(days=days)

    rows = db.execute(
        select(
            BloodUnit.discard_reason,
            func.count().label("units"),
            func.sum(BloodUnit.volume_ml).label("volume_ml"),
        )
        .where(
            BloodUnit.facility_id == facility_id,
            BloodUnit.discarded_at.is_not(None),
            BloodUnit.discarded_at >= since,
            # Separation is not wastage. The bag became its components; the
            # blood is still on the shelf under different numbers.
            BloodUnit.discard_reason != "SEPARATED_INTO_COMPONENTS",
        )
        .group_by(BloodUnit.discard_reason)
        .order_by(func.count().desc())
    ).all()

    catalogue = reasons()
    breakdown = []
    preventable = 0
    total = 0

    for row in rows:
        code = row.discard_reason or ""
        spec = catalogue.get(code, {})

        label = (
            spec.get("label")
            or SYSTEM_REASONS.get(code)
            or (code or "Unspecified").replace("_", " ").capitalize()
        )

        total += row.units

        if spec.get("preventable"):
            preventable += row.units

        breakdown.append(
            {
                "reason": row.discard_reason,
                "label": label,
                "units": row.units,
                "volume_ml": int(row.volume_ml or 0),
                "preventable": bool(spec.get("preventable", False)),
            }
        )

    return {
        "days": days,
        "total": total,
        "preventable": preventable,
        # The figure worth acting on. Everything else is the cost of doing the
        # work at all.
        "preventable_share": round(preventable / total, 3) if total else 0.0,
        "breakdown": breakdown,
    }
