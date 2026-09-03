"""Clinical sign-off for the deferral rules the sources disagree about.

Seven rules in `config/network.yaml` carry a `requires_clinical_signoff` entry.
They are there because the Punjab SOP and WHO give different answers, or because
the SOP contradicts itself — epilepsy appears on the permanent list and in the
temporary table three rows later; the same document gives 6 months post-delivery
in one row and 12 months for nursing mothers four rows down. Both cannot be right.

The config records which limb is applied and what the alternative was. What it
cannot do is decide for a particular donor, and a system that quietly applies a
default to a contested rule is making a clinical judgement while presenting it as
configuration.

So the deferral stands, and the case surfaces here for somebody with the
authority to weigh it. Lifting one is a recorded clinical act with a reason
attached, not a toggle.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Permission
from core import config
from core.clock import DEMO_DATETIME
from db.models import Donor, DonorDeferral, Facility
from services.audit import Actor, ServiceError, audited, require, snapshot

DEFERRAL_FIELDS = (
    "donor_id",
    "reason_code",
    "is_permanent",
    "deferred_at",
    "deferred_until",
    "lifted_at",
    "lifted_by",
)


def contested_rules() -> dict[str, dict]:
    """The rules needing a clinician, with both readings and the source note."""

    return dict(config.get("requires_clinical_signoff") or {})


def pending(
    db: Session, actor: Actor, *, facility_ids: list[str] | None = None
) -> list[dict]:
    """Open deferrals resting on a contested rule, oldest first.

    Oldest first on purpose: a donor deferred three weeks ago under a rule the
    sources disagree about has been kept off the register for three weeks by a
    config default nobody reviewed.
    """

    rules = contested_rules()
    facility_ids = (
        facility_ids if facility_ids is not None else readable_facilities(db, actor)
    )

    if not rules or not facility_ids:
        return []

    rows = db.execute(
        select(
            DonorDeferral.id,
            DonorDeferral.donor_id,
            DonorDeferral.reason_code,
            DonorDeferral.reason_note,
            DonorDeferral.is_permanent,
            DonorDeferral.deferred_at,
            DonorDeferral.deferred_until,
            DonorDeferral.recorded_by,
            Donor.full_name,
            Donor.donor_code,
            Donor.gender,
            Donor.registered_facility_id,
        )
        .join(Donor, Donor.id == DonorDeferral.donor_id)
        .where(
            DonorDeferral.reason_code.in_(list(rules)),
            DonorDeferral.lifted_at.is_(None),
            Donor.registered_facility_id.in_(facility_ids),
        )
        .order_by(DonorDeferral.deferred_at)
        .limit(200)
    ).all()

    return [
        {
            "deferral_id": row.id,
            "donor_id": row.donor_id,
            "donor_name": row.full_name,
            "donor_code": row.donor_code,
            "reason_code": row.reason_code,
            "reason_note": row.reason_note,
            "is_permanent": row.is_permanent,
            "deferred_at": row.deferred_at,
            "deferred_until": row.deferred_until,
            "recorded_by": row.recorded_by,
            "days_waiting": (DEMO_DATETIME.date() - row.deferred_at.date()).days,
            # Both limbs, so the reviewer sees the disagreement rather than only
            # the answer the config happened to pick.
            "rule": rules.get(row.reason_code, {}),
        }
        for row in rows
    ]


def pending_count(db: Session, facility_ids: list[str]) -> int:
    rules = contested_rules()

    if not rules or not facility_ids:
        return 0

    return (
        db.scalar(
            select(func.count())
            .select_from(DonorDeferral)
            .join(Donor, Donor.id == DonorDeferral.donor_id)
            .where(
                DonorDeferral.reason_code.in_(list(rules)),
                DonorDeferral.lifted_at.is_(None),
                Donor.registered_facility_id.in_(facility_ids),
            )
        )
        or 0
    )


def _own_deferral(db: Session, actor: Actor, deferral_id: str) -> DonorDeferral:
    record = db.scalars(
        select(DonorDeferral)
        .join(Donor, Donor.id == DonorDeferral.donor_id)
        .where(
            DonorDeferral.id == deferral_id,
            Donor.registered_facility_id.in_(readable_facilities(db, actor)),
        )
    ).first()

    if record is None:
        raise ServiceError("DEFERRAL_NOT_FOUND", "That deferral does not exist here.")

    return record


def readable_facilities(db: Session, actor: Actor) -> list[str]:
    """Facilities this actor may read AND act on.

    One function for both. When the queue was scoped by the caller and the
    action derived its own scope, the queue could offer a case that lifting
    then refused as "not found" — which reads to the user as a broken button.
    """

    if actor.facility_id:
        return [actor.facility_id]

    if actor.organization_id:
        return [
            row[0]
            for row in db.execute(
                select(Facility.id).where(
                    Facility.organization_id == actor.organization_id
                )
            ).all()
        ]

    return []


def lift(
    db: Session, actor: Actor, *, deferral_id: str, reason: str
) -> DonorDeferral:
    """Lift a contested deferral, on a recorded clinical judgement.

    The reason is mandatory and is not a dropdown. The whole point of flagging
    these rules is that the decision needs argument, and "approved" is not one.
    """

    require(actor, Permission.SIGN_OFF_DEFERRAL, "sign off clinical deferrals")

    reason = (reason or "").strip()

    if len(reason) < 12:
        raise ServiceError(
            "REASON_REQUIRED",
            "Record why this deferral is being lifted — a clinical decision "
            "needs its reasoning, not a note saying 'approved'.",
            field="reason",
        )

    record = _own_deferral(db, actor, deferral_id)

    if record.lifted_at is not None:
        raise ServiceError("ALREADY_LIFTED", "That deferral has already been lifted.")

    if record.reason_code not in contested_rules():
        # Only contested rules are reviewable here. Lifting a plain low
        # haemoglobin deferral is not a clinical judgement, it is a re-test.
        raise ServiceError(
            "NOT_CONTESTED",
            "That deferral does not rest on a contested rule and cannot be "
            "signed off here.",
        )

    before = snapshot(record, DEFERRAL_FIELDS)
    donor = db.get(Donor, record.donor_id)

    with audited(
        db, actor, "DEFERRAL_LIFTED", "donor_deferral", deferral_id
    ) as entry:
        record.lifted_at = DEMO_DATETIME
        record.lifted_by = actor.display_name
        record.reason_note = (
            f"{record.reason_note}\n" if record.reason_note else ""
        ) + f"Lifted by {actor.display_name}: {reason}"

        db.flush()
        _recompute_donor(db, donor)

        entry.on(record, before=before, after=snapshot(record, DEFERRAL_FIELDS))
        entry.note(
            clinical_reason=reason,
            rule=record.reason_code,
            applied_limb=contested_rules().get(record.reason_code, {}).get("applied"),
            alternative_limb=contested_rules()
            .get(record.reason_code, {})
            .get("alternative"),
        )

    return record


def uphold(
    db: Session, actor: Actor, *, deferral_id: str, reason: str
) -> DonorDeferral:
    """Confirm a contested deferral stands, with the reasoning recorded.

    Upholding writes as much as lifting does. A reviewed deferral and an
    unreviewed one look identical on the donor record otherwise, and the
    difference is the whole value of the queue.
    """

    require(actor, Permission.SIGN_OFF_DEFERRAL, "sign off clinical deferrals")

    reason = (reason or "").strip()

    if len(reason) < 12:
        raise ServiceError(
            "REASON_REQUIRED",
            "Record why this deferral stands.",
            field="reason",
        )

    record = _own_deferral(db, actor, deferral_id)
    before = snapshot(record, DEFERRAL_FIELDS)

    with audited(
        db, actor, "DEFERRAL_UPHELD", "donor_deferral", deferral_id
    ) as entry:
        record.reason_note = (
            f"{record.reason_note}\n" if record.reason_note else ""
        ) + f"Upheld by {actor.display_name}: {reason}"

        db.flush()

        entry.on(record, before=before, after=snapshot(record, DEFERRAL_FIELDS))
        entry.note(clinical_reason=reason, rule=record.reason_code)

    return record


def _recompute_donor(db: Session, donor: Donor) -> None:
    """Re-derive the donor's status from what is left in the ledger.

    Lifting one deferral does not make a donor available if another is still
    open, so the status is recomputed rather than set.
    """

    if donor is None:
        return

    open_rows = db.scalars(
        select(DonorDeferral).where(
            DonorDeferral.donor_id == donor.id,
            DonorDeferral.lifted_at.is_(None),
        )
    ).all()

    still_open = [
        row
        for row in open_rows
        if row.is_permanent
        or row.deferred_until is None
        or row.deferred_until > DEMO_DATETIME.date()
    ]

    if any(row.is_permanent for row in still_open):
        donor.is_permanently_deferred = True
        donor.deferred_until = None
        donor.availability_status = "PERMANENTLY_DEFERRED"
        return

    donor.is_permanently_deferred = False

    timed = [row.deferred_until for row in still_open if row.deferred_until]
    conditional = [row for row in still_open if row.deferred_until is None]

    if conditional:
        awaiting = any(
            (row.reason_code or "").startswith("TTI_AWAITING") for row in conditional
        )
        donor.deferred_until = None
        donor.availability_status = (
            "AWAITING_TTI_CONFIRMATION" if awaiting else "CONDITIONALLY_DEFERRED"
        )
    elif timed:
        donor.deferred_until = max(timed)
        donor.availability_status = "TEMPORARILY_DEFERRED"
    else:
        donor.deferred_until = None
        donor.availability_status = "AVAILABLE"
