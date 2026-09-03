"""Vein-to-vein traceability, in both directions.

This is what the whole chain exists to make possible, and it is two different
questions that need each other.

**Backward, from a unit.** A patient has a transfusion reaction. Which donor,
which session, which plate and kit lot, which separation, which fridge. You
cannot investigate a bag you cannot trace.

**Forward, from a donor.** That trace identifies a donor — or a donor's later
donation comes back reactive, or they phone in after giving to say they were
unwell. Now: every unit they have EVER given, and where each one is. Some are on
a shelf and can be pulled. Some went to another facility. Some have been
transfused, and those are the patients somebody has to look up.

Backward alone is the more obvious feature and the less useful one. It tells you
about a bag you are already holding. Forward is the one that protects people who
have not been harmed yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core.clock import DEMO_DATETIME
from db.models import (
    BloodGroup,
    BloodRequest,
    BloodUnit,
    Component,
    ComponentProduction,
    Crossmatch,
    Donation,
    DonationSession,
    DonationTest,
    Donor,
    DonorScreening,
    Facility,
    LabRun,
    StorageLocation,
    TemperatureLog,
    TransfusionRecord,
    UnitIssue,
)
from services.audit import Actor

# Where a unit can still be recovered from, versus where it cannot.
RECOVERABLE = ("AVAILABLE", "RESERVED", "CROSSMATCHED", "QUARANTINE")
GONE = ("TRANSFUSED", "ISSUED")


@dataclass
class TraceStep:
    """One link in the chain, in the order it happened."""

    stage: str
    happened_at: object
    summary: str
    detail: dict
    actor: str | None = None


def _readable(principal_or_actor) -> list[str]:
    facility_id = getattr(principal_or_actor, "facility_id", None)

    return [facility_id] if facility_id else []


def trace_unit(db: Session, actor: Actor, *, unit_id: str) -> dict | None:
    """Everything behind one unit, oldest first.

    Scoped to the actor's facility. A unit at another organisation is not
    traceable here — the network layer exchanges shared aggregates, never a
    donor identity.
    """

    unit = db.scalars(
        select(BloodUnit).where(
            BloodUnit.id == unit_id,
            BloodUnit.facility_id.in_(_readable(actor) or [""]),
        )
    ).first()

    if unit is None:
        return None

    component = db.get(Component, unit.component_id)
    group = db.get(BloodGroup, unit.blood_group_id) if unit.blood_group_id else None
    location = (
        db.get(StorageLocation, unit.storage_location_id)
        if unit.storage_location_id
        else None
    )

    steps: list[TraceStep] = []
    donation = db.get(Donation, unit.donation_id) if unit.donation_id else None
    donor = db.get(Donor, donation.donor_id) if donation else None
    screening = (
        db.get(DonorScreening, donation.screening_id)
        if donation and donation.screening_id
        else None
    )
    session_row = (
        db.get(DonationSession, donation.session_id)
        if donation and donation.session_id
        else None
    )

    if screening is not None:
        steps.append(
            TraceStep(
                stage="SCREENED",
                happened_at=screening.screened_at,
                summary="Donor screened and accepted",
                detail={
                    "haemoglobin_g_dl": screening.haemoglobin_g_dl,
                    "weight_kg": screening.weight_kg,
                    "outcome": screening.outcome,
                },
                actor=screening.screened_by,
            )
        )

    if donation is not None:
        steps.append(
            TraceStep(
                stage="COLLECTED",
                happened_at=donation.collected_at,
                summary=f"Collected as {donation.bag_type.lower()} bag, "
                f"{donation.volume_ml} ml",
                detail={
                    "din": donation.din,
                    "donation_type": donation.donation_type,
                    "anticoagulant": donation.anticoagulant,
                    "adverse_reaction": donation.adverse_reaction,
                    "session": session_row.name if session_row else None,
                    "session_code": session_row.session_code if session_row else None,
                },
                actor=donation.phlebotomist,
            )
        )

    # The lab. Grouped by plate, because the kit lot belongs to the run and a
    # recall asks about the lot.
    if donation is not None:
        results = db.execute(
            select(
                DonationTest.test_code,
                DonationTest.test_group,
                DonationTest.result,
                DonationTest.is_reactive,
                DonationTest.tested_at,
                DonationTest.tested_by,
                DonationTest.verified_by,
                DonationTest.method,
                DonationTest.kit_lot,
                LabRun.run_code,
            )
            .outerjoin(LabRun, LabRun.id == DonationTest.lab_run_id)
            .where(DonationTest.donation_id == donation.id)
            .order_by(DonationTest.tested_at, DonationTest.test_code)
        ).all()

        if results:
            screening_panel = [r for r in results if r.test_group == "TTI"]
            reactive = [r for r in results if r.is_reactive]

            steps.append(
                TraceStep(
                    stage="TESTED",
                    happened_at=min(r.tested_at for r in results),
                    summary=(
                        f"{len(screening_panel)} screening tests"
                        + (f", {len(reactive)} reactive" if reactive else ", all clear")
                    ),
                    detail={
                        "results": [
                            {
                                "code": r.test_code,
                                "group": r.test_group,
                                "result": r.result,
                                "reactive": bool(r.is_reactive),
                                "method": r.method,
                                "kit_lot": r.kit_lot,
                                "run_code": r.run_code,
                                "tested_by": r.tested_by,
                                "verified_by": r.verified_by,
                            }
                            for r in results
                        ]
                    },
                    actor=next((r.tested_by for r in results if r.tested_by), None),
                )
            )

        if donation.released_at:
            steps.append(
                TraceStep(
                    stage="RELEASED",
                    happened_at=donation.released_at,
                    summary="Released into issuable stock",
                    detail={},
                    actor=donation.released_by,
                )
            )

        production = db.scalars(
            select(ComponentProduction).where(
                ComponentProduction.donation_id == donation.id
            )
        ).first()

        if production is not None:
            shortfall = (production.units_expected or 0) - (
                production.units_produced or 0
            )

            steps.append(
                TraceStep(
                    stage="SEPARATED",
                    happened_at=production.produced_at,
                    summary=(
                        f"Separated by {production.method.lower().replace('_', ' ')}"
                        f" into {production.units_produced} components"
                        + (f", {shortfall} short" if shortfall > 0 else "")
                    ),
                    detail={
                        "expected": production.expected_components,
                        "produced": production.produced_components,
                        "losses": production.loss_reasons,
                        "minutes_from_collection": production.minutes_from_collection,
                    },
                    actor=production.produced_by,
                )
            )

    issues = list(
        db.scalars(
            select(UnitIssue)
            .where(UnitIssue.blood_unit_id == unit.id)
            .order_by(UnitIssue.issued_at)
        ).all()
    )

    for issue in issues:
        request = db.get(BloodRequest, issue.request_id)
        crossmatch = db.scalars(
            select(Crossmatch).where(
                Crossmatch.request_id == issue.request_id,
                Crossmatch.blood_unit_id == unit.id,
            )
        ).first()

        steps.append(
            TraceStep(
                stage="ISSUED",
                happened_at=issue.issued_at,
                summary=(
                    (
                        f"Emergency uncrossmatched release against {request.request_code}"
                        if issue.release_mode == "EMERGENCY_UNCROSSMATCHED"
                        else f"Issued against {request.request_code}"
                    )
                    if request is not None
                    else "Issued from stock"
                ),
                detail={
                    "issue_id": issue.id,
                    "request_code": request.request_code if request else None,
                    "patient_ref": request.patient_ref if request else None,
                    "destination_ward": issue.destination_ward,
                    "collected_by": issue.collected_by,
                    "crossmatch_id": crossmatch.id if crossmatch else None,
                    "crossmatch_method": crossmatch.method if crossmatch else None,
                    "release_mode": issue.release_mode,
                    "emergency_release_reason": issue.emergency_release_reason,
                    "emergency_authorized_by": issue.emergency_authorized_by,
                },
                actor=issue.issued_by,
            )
        )

        if issue.returned_at is not None:
            steps.append(
                TraceStep(
                    stage="RETURNED",
                    happened_at=issue.returned_at,
                    summary=(
                        "Returned and accepted back into stock"
                        if issue.return_accepted
                        else "Returned but rejected from stock"
                    ),
                    detail={
                        "accepted": issue.return_accepted,
                        "reason": issue.return_reason,
                        "minutes_out_of_storage": issue.minutes_out_of_storage,
                    },
                    actor=None,
                )
            )

        if issue.disposition == "NOT_RETURNED":
            steps.append(
                TraceStep(
                    stage="CUSTODY_EXCEPTION",
                    happened_at=issue.custody_closed_at,
                    summary="Issued unit was not returned or confirmed transfused",
                    detail={
                        "disposition": issue.disposition,
                        "investigation_notes": issue.custody_notes,
                    },
                    actor=None,
                )
            )

        transfusion = db.scalars(
            select(TransfusionRecord).where(TransfusionRecord.issue_id == issue.id)
        ).first()

        if transfusion is not None:
            steps.append(
                TraceStep(
                    stage="TRANSFUSED",
                    happened_at=transfusion.completed_at or transfusion.started_at,
                    summary=(
                        "Transfusion completed"
                        if transfusion.outcome == "COMPLETED"
                        else "Transfusion stopped"
                    ),
                    detail={
                        "outcome": transfusion.outcome,
                        "reaction_type": transfusion.reaction_type,
                        "reaction_severity": transfusion.reaction_severity,
                        "reaction_notes": transfusion.reaction_notes,
                        "reaction_reported_at": transfusion.reaction_reported_at,
                    },
                    actor=transfusion.recorded_by,
                )
            )

    # Legacy and generated history may predate the structured issue table. Keep
    # that gap visible instead of making the issue disappear from the trace.
    if unit.issued_at and not issues:
        steps.append(
            TraceStep(
                stage="ISSUED",
                happened_at=unit.issued_at,
                summary="Issued from stock (legacy record)",
                detail={"structured_issue_record": False},
                actor=None,
            )
        )

    if unit.discarded_at:
        steps.append(
            TraceStep(
                stage="DISCARDED",
                happened_at=unit.discarded_at,
                summary=(unit.discard_reason or "Discarded")
                .replace("_", " ")
                .capitalize(),
                detail={"reason": unit.discard_reason},
                actor=None,
            )
        )

    steps.sort(key=lambda step: step.happened_at or DEMO_DATETIME)

    return {
        "unit": unit,
        "component": component,
        "group": group,
        "donation": donation,
        "donor": donor,
        "session": session_row,
        "location": location,
        "steps": steps,
        "cold_chain": cold_chain_for(db, unit),
        # A gap in the chain is a fact worth stating rather than hiding: units
        # collected before go-live have no donation behind them.
        "complete": donation is not None,
    }


def cold_chain_for(db: Session, unit: BloodUnit) -> dict:
    """Excursions in the store this unit is sitting in, while it has been there.

    Only readings since the unit was collected count — a fridge that failed last
    month says nothing about a bag placed in it yesterday.
    """

    if not unit.storage_location_id:
        return {"location": None, "excursions": [], "readings": 0}

    since = unit.collected_at or (DEMO_DATETIME - timedelta(days=14))

    excursions = db.execute(
        select(
            TemperatureLog.recorded_at,
            TemperatureLog.temperature_c,
            TemperatureLog.source,
            TemperatureLog.action_taken,
        )
        .where(
            TemperatureLog.storage_location_id == unit.storage_location_id,
            TemperatureLog.is_out_of_range.is_(True),
            TemperatureLog.recorded_at >= since,
        )
        .order_by(TemperatureLog.recorded_at.desc())
        .limit(20)
    ).all()

    total = (
        db.scalar(
            select(func.count())
            .select_from(TemperatureLog)
            .where(
                TemperatureLog.storage_location_id == unit.storage_location_id,
                TemperatureLog.recorded_at >= since,
            )
        )
        or 0
    )

    return {
        "location": db.get(StorageLocation, unit.storage_location_id),
        "excursions": excursions,
        "readings": total,
    }


def units_in_store_during(
    db: Session, *, location_id: str, start, end
) -> list[BloodUnit]:
    """Which units were in this store while it was out of range.

    The query an excursion exists to answer. A unit counts if it was collected
    before the excursion ended and had not left before it began.
    """

    return list(
        db.scalars(
            select(BloodUnit)
            .where(
                BloodUnit.storage_location_id == location_id,
                BloodUnit.collected_at <= end,
                or_(
                    BloodUnit.issued_at.is_(None),
                    BloodUnit.issued_at >= start,
                ),
                or_(
                    BloodUnit.discarded_at.is_(None),
                    BloodUnit.discarded_at >= start,
                ),
            )
            .order_by(BloodUnit.din)
            .limit(500)
        ).all()
    )


def trace_donor(db: Session, actor: Actor, *, donor_id: str) -> dict | None:
    """Every unit this donor has ever given, and where each one went.

    The recall query. A donor whose later donation is reactive, or who calls in
    unwell after giving, needs every earlier unit found — the ones still on a
    shelf can be pulled, and the ones already transfused are patients somebody
    has to look up.
    """

    donor = db.scalars(
        select(Donor).where(
            Donor.id == donor_id,
            Donor.registered_facility_id.in_(_readable(actor) or [""]),
        )
    ).first()

    if donor is None:
        return None

    # `Donor` carries only `blood_group_id`, with no relationship behind it, so
    # a template asking for `donor.blood_group` gets silent Undefined and the
    # chip simply never appears. Resolved here instead.
    donor_group = (
        db.get(BloodGroup, donor.blood_group_id) if donor.blood_group_id else None
    )

    donations = db.scalars(
        select(Donation)
        .where(Donation.donor_id == donor_id)
        .order_by(Donation.collected_at.desc())
    ).all()

    # The register's lifetime count against what this system can actually trace.
    #
    # These differ legitimately and the difference must be stated, not hidden. A
    # donor's card may read eleven donations while one row exists here: the rest
    # predate go-live and were migrated as an opening balance, with no unit, no
    # test result and no bag behind them. During a recall that distinction is the
    # whole answer — ten of those units cannot be found because this system never
    # held them, and somebody has to go to the paper register for them.
    untraceable = max(0, (donor.total_donations or 0) - len(donations))

    if not donations:
        return {
            "donor": donor,
            "group": donor_group,
            "donations": [],
            "recoverable": [],
            "transfused": [],
            "gone": [],
            "summary": {
                "total": 0,
                "recoverable": 0,
                "transfused": 0,
                "other": 0,
                "before_go_live": untraceable,
            },
        }

    ids = [row.id for row in donations]

    units = db.execute(
        select(
            BloodUnit.id,
            BloodUnit.din,
            BloodUnit.status,
            BloodUnit.collected_at,
            BloodUnit.expires_at,
            BloodUnit.issued_at,
            BloodUnit.discard_reason,
            BloodUnit.donation_id,
            Component.code.label("component"),
            Facility.name_en.label("facility"),
            StorageLocation.name.label("location"),
        )
        .join(Component, Component.id == BloodUnit.component_id)
        .join(Facility, Facility.id == BloodUnit.facility_id)
        .outerjoin(
            StorageLocation, StorageLocation.id == BloodUnit.storage_location_id
        )
        .where(BloodUnit.donation_id.in_(ids))
        .order_by(BloodUnit.collected_at.desc())
    ).all()

    recoverable = [u for u in units if u.status in RECOVERABLE]
    transfused = [u for u in units if u.status in GONE]
    other = [u for u in units if u.status not in RECOVERABLE + GONE]

    return {
        "donor": donor,
        "group": donor_group,
        "donations": donations,
        "recoverable": recoverable,
        "transfused": transfused,
        "gone": other,
        "summary": {
            "total": len(units),
            "recoverable": len(recoverable),
            "transfused": len(transfused),
            "other": len(other),
            "before_go_live": untraceable,
        },
    }
