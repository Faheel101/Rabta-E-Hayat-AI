"""Clinical request, crossmatch, issue, return, and transfusion workflow.

The service owns the state machine. Routes render it, scripts may seed it, and
future REST/FHIR adapters call the same functions; none of those callers may
move inventory by assigning a status directly.

The important invariant is transactional: a unit, the request it fulfils, the
clinical record, and the audit entry change together or not at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, literal, select
from sqlalchemy.orm import Session

from app.auth import Permission
from core.clock import DEMO_DATETIME, as_utc
from core.config import get as config_get
from db.models import (
    BloodGroup,
    BloodRequest,
    BloodUnit,
    Compatibility,
    Component,
    Crossmatch,
    DemandEvent,
    Facility,
    TransfusionRecord,
    UnitIssue,
)
from services.audit import Actor, ServiceError, audited, require, snapshot


OPEN_REQUEST_STATUSES = ("PENDING", "CROSSMATCHED", "PARTIAL", "ISSUED")
FINAL_REQUEST_STATUSES = ("CLOSED", "CANCELLED")
URGENCIES = tuple(
    config_get(
        "clinical_operations.allowed_urgencies",
        ["ROUTINE", "URGENT", "EMERGENCY", "MASSIVE_TRANSFUSION"],
    )
)
CROSSMATCH_METHODS = tuple(
    config_get(
        "clinical_operations.allowed_crossmatch_methods",
        ["AHG_COOMBS", "GEL_CARD", "IMMEDIATE_SPIN", "ELECTRONIC"],
    )
)
REACTION_SEVERITIES = tuple(
    config_get(
        "clinical_operations.allowed_reaction_severities",
        ["MILD", "MODERATE", "SEVERE", "LIFE_THREATENING", "DEATH"],
    )
)
REACTION_TYPES = (
    "NONE",
    "FEBRILE_NON_HAEMOLYTIC",
    "ALLERGIC",
    "ANAPHYLACTIC",
    "ACUTE_HAEMOLYTIC",
    "DELAYED_HAEMOLYTIC",
    "TACO",
    "TRALI",
    "BACTERIAL_CONTAMINATION",
    "OTHER",
)
TRANSFUSION_OUTCOMES = ("COMPLETED", "STOPPED")
PATIENT_SEXES = ("FEMALE", "MALE", "OTHER", "UNKNOWN")
EMERGENCY_RELEASE_URGENCIES = tuple(
    config_get(
        "clinical_operations.emergency_release.allowed_urgencies",
        ["EMERGENCY", "MASSIVE_TRANSFUSION"],
    )
)
EMERGENCY_RELEASE_COMPONENTS = tuple(
    config_get(
        "clinical_operations.emergency_release.allowed_component_codes",
        ["PRBC"],
    )
)
EMERGENCY_UNKNOWN_RECIPIENT_GROUPS = tuple(
    config_get(
        "clinical_operations.emergency_release.unknown_recipient_group_codes",
        ["O-"],
    )
)

REQUEST_FIELDS = (
    "request_code",
    "facility_id",
    "patient_ref",
    "patient_age_years",
    "patient_sex",
    "patient_blood_group_id",
    "component_id",
    "units_requested",
    "units_issued",
    "urgency",
    "clinical_context",
    "replacement_units_required",
    "replacement_units_received",
    "replacement_waived",
    "replacement_waived_reason",
    "ward",
    "requested_by",
    "consultant",
    "required_by",
    "status",
    "was_substituted",
    "override_reason",
    "override_by",
    "closed_at",
    "notes",
)
CROSSMATCH_FIELDS = (
    "request_id",
    "blood_unit_id",
    "method",
    "result",
    "performed_at",
    "performed_by",
    "valid_until",
    "notes",
)
ISSUE_FIELDS = (
    "blood_unit_id",
    "request_id",
    "facility_id",
    "issued_at",
    "issued_by",
    "collected_by",
    "destination_ward",
    "release_mode",
    "emergency_release_reason",
    "emergency_authorized_by",
    "disposition",
    "custody_closed_at",
    "custody_notes",
    "returned_at",
    "return_accepted",
    "return_reason",
    "minutes_out_of_storage",
)
TRANSFUSION_FIELDS = (
    "blood_unit_id",
    "request_id",
    "issue_id",
    "started_at",
    "completed_at",
    "outcome",
    "reaction_type",
    "reaction_severity",
    "reaction_reported_at",
    "recorded_by",
)


def _text(value: str | None, *, maximum: int | None = None) -> str:
    cleaned = (value or "").strip()

    if maximum is not None and len(cleaned) > maximum:
        raise ServiceError(
            "VALUE_TOO_LONG",
            f"Keep this value to {maximum} characters or fewer.",
        )

    return cleaned


def _request_code(now: datetime) -> str:
    return f"BR-{now:%y%m%d}-{uuid.uuid4().hex[:6].upper()}"


def _own_request(db: Session, actor: Actor, request_id: str) -> BloodRequest:
    if not actor.facility_id:
        raise ServiceError("FACILITY_REQUIRED", "Select a facility first.")

    row = db.scalars(
        select(BloodRequest).where(
            BloodRequest.id == request_id,
            BloodRequest.facility_id == actor.facility_id,
        )
    ).first()

    if row is None:
        raise ServiceError("REQUEST_NOT_FOUND", "Clinical request not found.")

    return row


def _own_unit(db: Session, actor: Actor, unit_id: str) -> BloodUnit:
    if not actor.facility_id:
        raise ServiceError("FACILITY_REQUIRED", "Select a facility first.")

    unit = db.scalars(
        select(BloodUnit).where(
            BloodUnit.id == unit_id,
            BloodUnit.facility_id == actor.facility_id,
        )
    ).first()

    if unit is None:
        raise ServiceError("UNIT_NOT_FOUND", "Blood unit not found.")

    return unit


def _own_issue(db: Session, actor: Actor, issue_id: str) -> UnitIssue:
    issue = db.scalars(
        select(UnitIssue)
        .join(BloodRequest, BloodRequest.id == UnitIssue.request_id)
        .where(
            UnitIssue.id == issue_id,
            BloodRequest.facility_id == actor.facility_id,
        )
    ).first()

    if issue is None:
        raise ServiceError("ISSUE_NOT_FOUND", "Unit issue record not found.")

    return issue


def _assert_open(request: BloodRequest) -> None:
    if request.status not in OPEN_REQUEST_STATUSES:
        raise ServiceError(
            "REQUEST_FINAL",
            "This request is already closed or cancelled.",
        )


def _active_crossmatch_count(
    db: Session, request_id: str, *, now: datetime = DEMO_DATETIME
) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Crossmatch)
            .join(BloodUnit, BloodUnit.id == Crossmatch.blood_unit_id)
            .where(
                Crossmatch.request_id == request_id,
                Crossmatch.result == "COMPATIBLE",
                Crossmatch.valid_until >= now,
                BloodUnit.status == "CROSSMATCHED",
            )
        )
        or 0
    )


def _demand_outcome(request: BloodRequest) -> str:
    if request.status == "CANCELLED":
        return "CANCELLED"
    if request.units_issued >= request.units_requested:
        return "FULFILLED"
    if request.units_issued > 0:
        return "PARTIAL"
    return "UNFULFILLED"


def _sync_demand_event(
    db: Session,
    request: BloodRequest,
    *,
    now: datetime = DEMO_DATETIME,
) -> DemandEvent | None:
    """Mirror one clinical request into canonical, identity-free demand truth.

    An unknown patient group remains visible in the clinical request queue but
    is not assigned to an invented forecasting group.  Once the group is
    recorded, the canonical event is created automatically.
    """

    event = db.scalar(
        select(DemandEvent).where(DemandEvent.blood_request_id == request.id)
    )

    if request.patient_blood_group_id is None:
        if event is not None:
            db.delete(event)
        return None

    if event is None:
        event = DemandEvent(
            blood_request_id=request.id,
            facility_id=request.facility_id,
            component_id=request.component_id,
            blood_group_id=request.patient_blood_group_id,
            requested_at=request.requested_at,
            source_system_ref=f"clinical-request:{request.id}",
        )
        db.add(event)

    event.facility_id = request.facility_id
    event.component_id = request.component_id
    event.blood_group_id = request.patient_blood_group_id
    event.requested_at = request.requested_at
    event.units_requested = request.units_requested
    event.units_issued = request.units_issued
    event.urgency = request.urgency
    event.clinical_context = request.clinical_context
    event.was_substituted = request.was_substituted
    event.outcome = _demand_outcome(request)
    event.last_synced_at = now
    return event


def sync_clinical_demand_events(
    db: Session, *, now: datetime = DEMO_DATETIME
) -> dict[str, object]:
    """Backfill and reconcile canonical demand for every clinical request.

    Normal workflow writes stay synchronized transactionally. This bounded
    reconciliation exists for pre-Sprint-11 requests and additive migrations;
    it never copies the patient reference or other identifying fields.
    """

    requests = list(db.scalars(select(BloodRequest)).all())
    linked = {
        event.blood_request_id: event
        for event in db.scalars(
            select(DemandEvent).where(DemandEvent.blood_request_id.is_not(None))
        ).all()
    }
    created = 0
    removed = 0

    for request in requests:
        before = linked.get(request.id)
        after = _sync_demand_event(db, request, now=now)
        if before is None and after is not None:
            created += 1
        elif before is not None and after is None:
            removed += 1

    db.flush()
    return {
        "clinical_requests_seen": len(requests),
        "clinical_demand_created": created,
        "clinical_demand_removed": removed,
        "series_keys": sorted(
            {
                (
                    request.facility_id,
                    request.component_id,
                    request.patient_blood_group_id,
                )
                for request in requests
                if request.patient_blood_group_id is not None
            }
        ),
    }


def _sync_status(
    db: Session, request: BloodRequest, *, now: datetime = DEMO_DATETIME
) -> None:
    if request.status in FINAL_REQUEST_STATUSES:
        _sync_demand_event(db, request, now=now)
        return

    if request.units_issued >= request.units_requested:
        request.status = "ISSUED"
    elif request.units_issued > 0:
        request.status = "PARTIAL"
    elif _active_crossmatch_count(db, request.id, now=now) > 0:
        request.status = "CROSSMATCHED"
    else:
        request.status = "PENDING"

    _sync_demand_event(db, request, now=now)


def _unresolved_issue_count(db: Session, request_id: str) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(UnitIssue)
            .where(
                UnitIssue.request_id == request_id,
                UnitIssue.custody_closed_at.is_(None),
            )
        )
        or 0
    )


def create_request(
    db: Session,
    actor: Actor,
    *,
    patient_ref: str,
    component_id: int,
    units_requested: int,
    urgency: str,
    clinical_context: str,
    patient_blood_group_id: int | None = None,
    patient_age_years: int | None = None,
    patient_sex: str | None = None,
    ward: str | None = None,
    requested_by: str | None = None,
    consultant: str | None = None,
    required_by: datetime | None = None,
    replacement_units_required: int = 0,
    notes: str | None = None,
    now: datetime = DEMO_DATETIME,
) -> BloodRequest:
    """Create a pseudonymous clinical requirement at the actor's facility."""

    require(actor, Permission.MANAGE_CLINICAL_REQUEST, "create clinical requests")

    if not actor.facility_id or db.get(Facility, actor.facility_id) is None:
        raise ServiceError("FACILITY_REQUIRED", "Select a valid facility first.")

    patient_ref = _text(patient_ref, maximum=60)

    if len(patient_ref) < 3:
        raise ServiceError(
            "PATIENT_REF_REQUIRED",
            "Enter a pseudonymous patient or episode reference.",
            field="patient_ref",
        )

    component = db.get(Component, component_id)

    if component is None:
        raise ServiceError("COMPONENT_REQUIRED", "Select a valid component.")

    if patient_blood_group_id is not None and db.get(
        BloodGroup, patient_blood_group_id
    ) is None:
        raise ServiceError("GROUP_REQUIRED", "Select a valid patient blood group.")

    maximum_units = int(
        config_get("clinical_operations.max_units_per_request", 20)
    )

    if units_requested < 1 or units_requested > maximum_units:
        raise ServiceError(
            "UNITS_OUT_OF_RANGE",
            f"Request between 1 and {maximum_units} units.",
            field="units_requested",
        )

    urgency = _text(urgency).upper()

    if urgency not in URGENCIES:
        raise ServiceError("URGENCY_INVALID", "Select a valid urgency.")

    sex = _text(patient_sex).upper() or "UNKNOWN"

    if sex not in PATIENT_SEXES:
        raise ServiceError("SEX_INVALID", "Select a valid patient sex.")

    if patient_age_years is not None and not 0 <= patient_age_years <= 130:
        raise ServiceError(
            "AGE_INVALID",
            "Patient age must be between 0 and 130 years.",
            field="patient_age_years",
        )

    if replacement_units_required < 0 or replacement_units_required > units_requested:
        raise ServiceError(
            "REPLACEMENT_INVALID",
            "Replacement units cannot exceed units requested.",
        )

    required_by = as_utc(required_by)

    if required_by is not None and required_by < as_utc(now):
        raise ServiceError(
            "REQUIRED_BY_PAST",
            "Required-by time cannot be earlier than the request time.",
            field="required_by",
        )

    record = BloodRequest(
        request_code=_request_code(now),
        facility_id=actor.facility_id,
        patient_ref=patient_ref,
        patient_age_years=patient_age_years,
        patient_sex=sex,
        patient_blood_group_id=patient_blood_group_id,
        component_id=component_id,
        units_requested=units_requested,
        units_issued=0,
        urgency=urgency,
        clinical_context=_text(clinical_context, maximum=40).upper() or "OTHER",
        replacement_units_required=replacement_units_required,
        replacement_units_received=0,
        ward=_text(ward, maximum=120) or None,
        requested_by=_text(requested_by, maximum=160) or None,
        consultant=_text(consultant, maximum=160) or None,
        requested_at=now,
        required_by=required_by,
        status="PENDING",
        notes=_text(notes) or None,
    )

    with audited(db, actor, "BLOOD_REQUEST_CREATED", "blood_request") as entry:
        db.add(record)
        db.flush()
        demand_event = _sync_demand_event(db, record, now=now)
        db.flush()
        entry.on(record, after=snapshot(record, REQUEST_FIELDS))
        entry.note(
            demand_event_id=getattr(demand_event, "id", None),
            demand_classified=demand_event is not None,
        )

    return record


def update_request(
    db: Session,
    actor: Actor,
    *,
    request_id: str,
    patient_ref: str,
    component_id: int,
    units_requested: int,
    urgency: str,
    clinical_context: str,
    patient_blood_group_id: int | None = None,
    patient_age_years: int | None = None,
    patient_sex: str | None = None,
    ward: str | None = None,
    requested_by: str | None = None,
    consultant: str | None = None,
    required_by: datetime | None = None,
    replacement_units_required: int = 0,
    notes: str | None = None,
    now: datetime = DEMO_DATETIME,
) -> BloodRequest:
    """Edit an open request without invalidating an allocation or issue."""

    require(actor, Permission.MANAGE_CLINICAL_REQUEST, "edit clinical requests")
    request = _own_request(db, actor, request_id)
    _assert_open(request)

    patient_ref = _text(patient_ref, maximum=60)
    if len(patient_ref) < 3:
        raise ServiceError(
            "PATIENT_REF_REQUIRED",
            "Enter a pseudonymous patient or episode reference.",
            field="patient_ref",
        )

    if db.get(Component, component_id) is None:
        raise ServiceError("COMPONENT_REQUIRED", "Select a valid component.")

    if patient_blood_group_id is not None and db.get(
        BloodGroup, patient_blood_group_id
    ) is None:
        raise ServiceError("GROUP_REQUIRED", "Select a valid patient blood group.")

    maximum_units = int(config_get("clinical_operations.max_units_per_request", 20))
    if units_requested < 1 or units_requested > maximum_units:
        raise ServiceError(
            "UNITS_OUT_OF_RANGE",
            f"Request between 1 and {maximum_units} units.",
            field="units_requested",
        )

    active_allocations = _active_crossmatch_count(db, request.id, now=now)
    committed_units = request.units_issued + active_allocations
    if units_requested < committed_units:
        raise ServiceError(
            "UNITS_BELOW_COMMITTED",
            "Release allocated units before reducing the requested quantity.",
            field="units_requested",
        )

    identity_changed = (
        component_id != request.component_id
        or patient_blood_group_id != request.patient_blood_group_id
    )
    if identity_changed and committed_units:
        raise ServiceError(
            "ALLOCATED_REQUEST_IDENTITY",
            "Release allocated units before changing the component or blood group.",
        )

    urgency = _text(urgency).upper()
    if urgency not in URGENCIES:
        raise ServiceError("URGENCY_INVALID", "Select a valid urgency.")

    sex = _text(patient_sex).upper() or "UNKNOWN"
    if sex not in PATIENT_SEXES:
        raise ServiceError("SEX_INVALID", "Select a valid patient sex.")

    if patient_age_years is not None and not 0 <= patient_age_years <= 130:
        raise ServiceError(
            "AGE_INVALID",
            "Patient age must be between 0 and 130 years.",
            field="patient_age_years",
        )

    if (
        replacement_units_required < request.replacement_units_received
        or replacement_units_required > units_requested
    ):
        raise ServiceError(
            "REPLACEMENT_INVALID",
            "Replacement requirement must cover receipts and cannot exceed units requested.",
        )

    required_by = as_utc(required_by)
    if required_by is not None and required_by < as_utc(now):
        raise ServiceError(
            "REQUIRED_BY_PAST",
            "Required-by time cannot be earlier than the request time.",
            field="required_by",
        )

    before = snapshot(request, REQUEST_FIELDS)
    with audited(
        db, actor, "BLOOD_REQUEST_UPDATED", "blood_request", request.id
    ) as entry:
        request.patient_ref = patient_ref
        request.patient_age_years = patient_age_years
        request.patient_sex = sex
        request.patient_blood_group_id = patient_blood_group_id
        request.component_id = component_id
        request.units_requested = units_requested
        request.urgency = urgency
        request.clinical_context = (
            _text(clinical_context, maximum=40).upper() or "OTHER"
        )
        request.replacement_units_required = replacement_units_required
        request.ward = _text(ward, maximum=120) or None
        request.requested_by = _text(requested_by, maximum=160) or None
        request.consultant = _text(consultant, maximum=160) or None
        request.required_by = required_by
        request.notes = _text(notes) or None

        if identity_changed:
            request.was_substituted = False
            request.override_reason = None
            request.override_by = None

        _sync_status(db, request, now=now)
        entry.on(request, before=before, after=snapshot(request, REQUEST_FIELDS))

    return request


def candidate_units(
    db: Session,
    actor: Actor,
    *,
    request_id: str,
    include_override: bool = True,
    limit: int = 40,
    now: datetime = DEMO_DATETIME,
):
    """Return compatible FEFO stock, identical groups first.

    `requires_override` is returned to the caller; it is never silently treated
    as ordinary compatibility.
    """

    request = _own_request(db, actor, request_id)

    if request.patient_blood_group_id is None:
        return []

    statement = (
        select(
            BloodUnit.id,
            BloodUnit.din,
            BloodUnit.expires_at,
            BloodUnit.volume_ml,
            BloodUnit.is_leucodepleted,
            BloodUnit.is_irradiated,
            BloodGroup.code.label("donor_group_code"),
            Compatibility.preference_rank,
            Compatibility.requires_override,
        )
        .join(BloodGroup, BloodGroup.id == BloodUnit.blood_group_id)
        .join(
            Compatibility,
            (Compatibility.component_id == BloodUnit.component_id)
            & (Compatibility.donor_group_id == BloodUnit.blood_group_id),
        )
        .where(
            BloodUnit.facility_id == request.facility_id,
            BloodUnit.component_id == request.component_id,
            BloodUnit.status == "AVAILABLE",
            BloodUnit.screening_status == "PASSED",
            BloodUnit.expires_at > now,
            BloodUnit.cold_chain_breach_count == 0,
            Compatibility.recipient_group_id == request.patient_blood_group_id,
            Compatibility.is_compatible.is_(True),
        )
    )

    if not include_override:
        statement = statement.where(Compatibility.requires_override.is_(False))

    return db.execute(
        statement.order_by(
            Compatibility.preference_rank,
            BloodUnit.expires_at,
            BloodUnit.din,
        ).limit(max(1, min(limit, 200)))
    ).all()


def emergency_candidate_units(
    db: Session,
    actor: Actor,
    *,
    request_id: str,
    limit: int = 20,
    now: datetime = DEMO_DATETIME,
):
    """Return policy-eligible FEFO stock for an emergency-release exception."""

    request = _own_request(db, actor, request_id)
    component = db.get(Component, request.component_id)

    if (
        request.urgency not in EMERGENCY_RELEASE_URGENCIES
        or component is None
        or component.code not in EMERGENCY_RELEASE_COMPONENTS
    ):
        return []

    if request.patient_blood_group_id is not None:
        return candidate_units(
            db,
            actor,
            request_id=request.id,
            limit=limit,
            now=now,
        )

    statement = (
        select(
            BloodUnit.id,
            BloodUnit.din,
            BloodUnit.expires_at,
            BloodUnit.volume_ml,
            BloodUnit.is_leucodepleted,
            BloodUnit.is_irradiated,
            BloodGroup.code.label("donor_group_code"),
            literal(1).label("preference_rank"),
            literal(True).label("requires_override"),
        )
        .join(BloodGroup, BloodGroup.id == BloodUnit.blood_group_id)
        .where(
            BloodUnit.facility_id == request.facility_id,
            BloodUnit.component_id == request.component_id,
            BloodUnit.status == "AVAILABLE",
            BloodUnit.screening_status == "PASSED",
            BloodUnit.expires_at > now,
            BloodUnit.cold_chain_breach_count == 0,
            BloodGroup.code.in_(EMERGENCY_UNKNOWN_RECIPIENT_GROUPS),
        )
        .order_by(BloodUnit.expires_at, BloodUnit.din)
        .limit(max(1, min(limit, 100)))
    )
    return db.execute(statement).all()


def expire_crossmatches(
    db: Session,
    actor: Actor,
    *,
    facility_id: str | None = None,
    now: datetime = DEMO_DATETIME,
) -> int:
    """Release expired allocations so stale crossmatches cannot trap stock."""

    if actor.role != "SYSTEM":
        require(actor, Permission.PERFORM_CROSSMATCH, "expire crossmatches")
        facility_id = actor.facility_id

    statement = (
        select(Crossmatch, BloodUnit)
        .join(BloodUnit, BloodUnit.id == Crossmatch.blood_unit_id)
        .where(
            Crossmatch.result == "COMPATIBLE",
            Crossmatch.valid_until.is_not(None),
            Crossmatch.valid_until < now,
            BloodUnit.status == "CROSSMATCHED",
        )
    )
    if facility_id:
        statement = statement.where(BloodUnit.facility_id == facility_id)

    rows = db.execute(statement.order_by(Crossmatch.valid_until)).all()
    if not rows:
        return 0

    affected_requests: dict[str, BloodRequest] = {}
    expired_ids: list[str] = []

    with audited(
        db,
        actor,
        "CROSSMATCHES_EXPIRED",
        "crossmatch_batch",
        facility_id or "ALL_FACILITIES",
    ) as entry:
        for crossmatch, unit in rows:
            newer_active = db.scalars(
                select(Crossmatch).where(
                    Crossmatch.blood_unit_id == unit.id,
                    Crossmatch.id != crossmatch.id,
                    Crossmatch.result == "COMPATIBLE",
                    Crossmatch.valid_until >= now,
                )
            ).first()
            if newer_active is not None:
                continue

            crossmatch.result = "EXPIRED"
            unit.status = "AVAILABLE"
            expired_ids.append(crossmatch.id)
            request = db.get(BloodRequest, crossmatch.request_id)
            if request is not None:
                affected_requests[request.id] = request

        db.flush()
        for request in affected_requests.values():
            _sync_status(db, request, now=now)

        entry.on(
            before={"expired_count": 0},
            after={"expired_count": len(expired_ids)},
            entity_id=facility_id or "ALL_FACILITIES",
        )
        entry.note(
            crossmatch_ids=expired_ids,
            request_ids=sorted(affected_requests),
        )

    return len(expired_ids)


def record_crossmatch(
    db: Session,
    actor: Actor,
    *,
    request_id: str,
    unit_id: str,
    result: str,
    method: str,
    notes: str | None = None,
    override_reason: str | None = None,
    now: datetime = DEMO_DATETIME,
) -> Crossmatch:
    """Record or repeat the lab work for a request-unit pair."""

    require(actor, Permission.PERFORM_CROSSMATCH, "perform crossmatches")
    request = _own_request(db, actor, request_id)
    unit = _own_unit(db, actor, unit_id)
    _assert_open(request)

    if request.patient_blood_group_id is None:
        raise ServiceError(
            "GROUP_REQUIRED",
            "Record the patient blood group before crossmatching.",
        )

    if unit.component_id != request.component_id:
        raise ServiceError(
            "COMPONENT_MISMATCH",
            "The unit component does not match this request.",
        )

    result = _text(result).upper()

    if result not in ("COMPATIBLE", "INCOMPATIBLE"):
        raise ServiceError("RESULT_INVALID", "Select a valid crossmatch result.")

    method = _text(method).upper()

    if method not in CROSSMATCH_METHODS:
        raise ServiceError("METHOD_INVALID", "Select a valid crossmatch method.")

    existing = db.scalars(
        select(Crossmatch).where(
            Crossmatch.request_id == request.id,
            Crossmatch.blood_unit_id == unit.id,
        )
    ).first()

    if unit.status == "CROSSMATCHED":
        allocation = db.scalars(
            select(Crossmatch)
            .where(
                Crossmatch.blood_unit_id == unit.id,
                Crossmatch.result == "COMPATIBLE",
            )
            .order_by(Crossmatch.performed_at.desc())
        ).first()

        if allocation is None or allocation.request_id != request.id:
            raise ServiceError(
                "UNIT_ALLOCATED",
                "This unit is crossmatched to another request.",
            )

    if unit.status not in ("AVAILABLE", "CROSSMATCHED"):
        raise ServiceError(
            "UNIT_UNAVAILABLE",
            f"A unit in {unit.status.lower()} status cannot be crossmatched.",
        )

    mapping = db.scalars(
        select(Compatibility).where(
            Compatibility.component_id == request.component_id,
            Compatibility.recipient_group_id == request.patient_blood_group_id,
            Compatibility.donor_group_id == unit.blood_group_id,
            Compatibility.is_compatible.is_(True),
        )
    ).first()

    if result == "COMPATIBLE" and mapping is None:
        raise ServiceError(
            "ABO_RH_INCOMPATIBLE",
            "This donor-recipient group combination is not permitted.",
        )

    override_reason = _text(override_reason)

    if result == "COMPATIBLE" and mapping.requires_override:
        if len(override_reason) < 12:
            raise ServiceError(
                "OVERRIDE_REASON_REQUIRED",
                "Record the clinical justification for this compatibility override.",
                field="override_reason",
            )

    if result == "COMPATIBLE" and existing is None:
        remaining = request.units_requested - request.units_issued

        if _active_crossmatch_count(db, request.id, now=now) >= remaining:
            raise ServiceError(
                "REQUEST_ALLOCATED",
                "This request already has enough active crossmatched units.",
            )

    valid_hours = int(
        config_get("clinical_operations.crossmatch_valid_hours", 72)
    )
    record = existing or Crossmatch(
        request_id=request.id,
        blood_unit_id=unit.id,
    )
    before = snapshot(record, CROSSMATCH_FIELDS) if existing else None
    unit_before = unit.status
    request_before = request.status

    with audited(
        db,
        actor,
        "CROSSMATCH_RECORDED" if existing is None else "CROSSMATCH_REPEATED",
        "crossmatch",
    ) as entry:
        record.method = method
        record.result = result
        record.performed_at = now
        record.performed_by = actor.display_name
        record.valid_until = (
            now + timedelta(hours=valid_hours) if result == "COMPATIBLE" else None
        )
        record.notes = _text(notes) or None

        if existing is None:
            db.add(record)

        if result == "COMPATIBLE":
            unit.status = "CROSSMATCHED"

            recipient = db.get(BloodGroup, request.patient_blood_group_id)
            donor = db.get(BloodGroup, unit.blood_group_id)

            if recipient and donor and recipient.code != donor.code:
                request.was_substituted = True

            if mapping.requires_override:
                request.override_reason = override_reason
                request.override_by = actor.display_name
        else:
            unit.status = "AVAILABLE"

        db.flush()
        _sync_status(db, request, now=now)
        entry.on(
            record,
            before=before,
            after=snapshot(record, CROSSMATCH_FIELDS),
        )
        entry.note(
            request_id=request.id,
            request_status_from=request_before,
            request_status_to=request.status,
            unit_status_from=unit_before,
            unit_status_to=unit.status,
            compatibility_rank=getattr(mapping, "preference_rank", None),
            required_override=bool(getattr(mapping, "requires_override", False)),
            override_reason=override_reason or None,
        )

    return record


def release_crossmatch(
    db: Session,
    actor: Actor,
    *,
    request_id: str,
    unit_id: str,
    reason: str,
    now: datetime = DEMO_DATETIME,
) -> Crossmatch:
    """Release a compatible allocation without cancelling the requirement."""

    require(actor, Permission.PERFORM_CROSSMATCH, "release crossmatched units")
    request = _own_request(db, actor, request_id)
    unit = _own_unit(db, actor, unit_id)
    _assert_open(request)
    reason = _text(reason)

    if len(reason) < 12:
        raise ServiceError(
            "RELEASE_REASON_REQUIRED",
            "Record why this crossmatched unit is being released.",
            field="reason",
        )

    record = db.scalars(
        select(Crossmatch).where(
            Crossmatch.request_id == request.id,
            Crossmatch.blood_unit_id == unit.id,
            Crossmatch.result == "COMPATIBLE",
        )
    ).first()

    if record is None or unit.status != "CROSSMATCHED":
        raise ServiceError(
            "NO_ACTIVE_CROSSMATCH",
            "This request has no active allocation for that unit.",
        )

    before = snapshot(record, CROSSMATCH_FIELDS)
    request_before = request.status

    with audited(db, actor, "CROSSMATCH_RELEASED", "crossmatch", record.id) as entry:
        record.result = "RELEASED"
        record.valid_until = now
        record.notes = f"{record.notes}\n" if record.notes else ""
        record.notes += f"Released: {reason}"
        unit.status = "AVAILABLE"
        db.flush()
        _sync_status(db, request, now=now)
        entry.on(
            record,
            before=before,
            after=snapshot(record, CROSSMATCH_FIELDS),
        )
        entry.note(
            reason=reason,
            unit_status_to=unit.status,
            request_status_from=request_before,
            request_status_to=request.status,
        )

    return record


def issue_unit(
    db: Session,
    actor: Actor,
    *,
    request_id: str,
    unit_id: str,
    collected_by: str,
    patient_ref_confirmation: str,
    destination_ward: str | None = None,
    now: datetime = DEMO_DATETIME,
) -> UnitIssue:
    """Hand one validly crossmatched unit out of controlled inventory."""

    require(actor, Permission.ISSUE_UNIT, "issue blood units")
    request = _own_request(db, actor, request_id)
    unit = _own_unit(db, actor, unit_id)
    _assert_open(request)

    if request.units_issued >= request.units_requested:
        raise ServiceError("REQUEST_FILLED", "This request is already fully issued.")

    if unit.status != "CROSSMATCHED":
        raise ServiceError(
            "NOT_CROSSMATCHED",
            "The unit must have a valid compatible crossmatch before issue.",
        )

    crossmatch = db.scalars(
        select(Crossmatch).where(
            Crossmatch.request_id == request.id,
            Crossmatch.blood_unit_id == unit.id,
            Crossmatch.result == "COMPATIBLE",
        )
    ).first()

    if (
        crossmatch is None
        or crossmatch.valid_until is None
        or as_utc(crossmatch.valid_until) < as_utc(now)
    ):
        raise ServiceError(
            "CROSSMATCH_EXPIRED",
            "The crossmatch has expired and must be repeated before issue.",
        )

    if unit.screening_status != "PASSED" or as_utc(unit.expires_at) <= as_utc(now):
        raise ServiceError(
            "UNIT_UNSAFE",
            "The unit is not currently eligible for issue.",
        )

    if unit.cold_chain_breach_count:
        raise ServiceError(
            "COLD_CHAIN_BREACH",
            "A unit with a cold-chain breach cannot be issued.",
        )

    collected_by = _text(collected_by, maximum=160)

    if len(collected_by) < 3:
        raise ServiceError(
            "COLLECTOR_REQUIRED",
            "Record the person receiving custody of the unit.",
            field="collected_by",
        )

    if _text(patient_ref_confirmation) != request.patient_ref:
        raise ServiceError(
            "PATIENT_REF_MISMATCH",
            "The patient or episode reference does not match this request.",
            field="patient_ref_confirmation",
        )

    issue = UnitIssue(
        blood_unit_id=unit.id,
        request_id=request.id,
        facility_id=request.facility_id,
        issued_at=now,
        issued_by=actor.display_name,
        collected_by=collected_by,
        destination_ward=_text(destination_ward, maximum=120) or request.ward,
        release_mode="CROSSMATCHED",
        disposition="AWAITING_OUTCOME",
    )
    unit_before = unit.status
    request_before = snapshot(request, ("units_issued", "status"))

    with audited(db, actor, "UNIT_ISSUED", "unit_issue") as entry:
        db.add(issue)
        unit.status = "ISSUED"
        unit.issued_at = now
        request.units_issued += 1
        db.flush()
        _sync_status(db, request, now=now)
        entry.on(issue, after=snapshot(issue, ISSUE_FIELDS))
        entry.note(
            request_before=request_before,
            request_after=snapshot(request, ("units_issued", "status")),
            unit_status_from=unit_before,
            unit_status_to=unit.status,
            crossmatch_id=crossmatch.id,
        )

    return issue


def emergency_issue_unit(
    db: Session,
    actor: Actor,
    *,
    request_id: str,
    unit_id: str,
    collected_by: str,
    patient_ref_confirmation: str,
    emergency_reason: str,
    authorized_by: str,
    acknowledge_uncrossmatched: bool,
    destination_ward: str | None = None,
    now: datetime = DEMO_DATETIME,
) -> UnitIssue:
    """Issue eligible stock before crossmatch under a governed emergency exception."""

    require(actor, Permission.ISSUE_UNIT, "perform emergency blood release")
    request = _own_request(db, actor, request_id)
    unit = _own_unit(db, actor, unit_id)
    _assert_open(request)

    if request.urgency not in EMERGENCY_RELEASE_URGENCIES:
        raise ServiceError(
            "EMERGENCY_URGENCY_REQUIRED",
            "Emergency release is limited to emergency or massive-transfusion requests.",
        )

    component = db.get(Component, request.component_id)
    if component is None or component.code not in EMERGENCY_RELEASE_COMPONENTS:
        raise ServiceError(
            "EMERGENCY_COMPONENT_NOT_ALLOWED",
            "This component is not enabled for uncrossmatched emergency release.",
        )

    if request.units_issued >= request.units_requested:
        raise ServiceError("REQUEST_FILLED", "This request is already fully issued.")

    if unit.component_id != request.component_id:
        raise ServiceError(
            "COMPONENT_MISMATCH",
            "The unit component does not match this request.",
        )

    if unit.status != "AVAILABLE":
        raise ServiceError(
            "UNIT_UNAVAILABLE",
            "Only an available unit can be released through the emergency pathway.",
        )

    if unit.screening_status != "PASSED" or as_utc(unit.expires_at) <= as_utc(now):
        raise ServiceError(
            "UNIT_UNSAFE",
            "The unit is not currently eligible for issue.",
        )

    if unit.cold_chain_breach_count:
        raise ServiceError(
            "COLD_CHAIN_BREACH",
            "A unit with a cold-chain breach cannot be issued.",
        )

    donor_group = db.get(BloodGroup, unit.blood_group_id)
    compatibility = None
    if request.patient_blood_group_id is None:
        if (
            donor_group is None
            or donor_group.code not in EMERGENCY_UNKNOWN_RECIPIENT_GROUPS
        ):
            raise ServiceError(
                "UNKNOWN_GROUP_POLICY",
                "This unit is not enabled for emergency release to an unknown group.",
            )
    else:
        compatibility = db.scalars(
            select(Compatibility).where(
                Compatibility.component_id == request.component_id,
                Compatibility.recipient_group_id == request.patient_blood_group_id,
                Compatibility.donor_group_id == unit.blood_group_id,
                Compatibility.is_compatible.is_(True),
            )
        ).first()
        if compatibility is None:
            raise ServiceError(
                "ABO_RH_INCOMPATIBLE",
                "This donor-recipient group combination is not permitted.",
            )

    collected_by = _text(collected_by, maximum=160)
    authorized_by = _text(authorized_by, maximum=160)
    emergency_reason = _text(emergency_reason)

    if len(collected_by) < 3:
        raise ServiceError(
            "COLLECTOR_REQUIRED",
            "Record the person receiving custody of the unit.",
            field="collected_by",
        )
    if _text(patient_ref_confirmation) != request.patient_ref:
        raise ServiceError(
            "PATIENT_REF_MISMATCH",
            "The patient or episode reference does not match this request.",
            field="patient_ref_confirmation",
        )
    if len(authorized_by) < 3:
        raise ServiceError(
            "EMERGENCY_AUTHORIZATION_REQUIRED",
            "Record the clinician authorizing emergency release.",
            field="authorized_by",
        )
    if len(emergency_reason) < 12:
        raise ServiceError(
            "EMERGENCY_REASON_REQUIRED",
            "Record the clinical justification for emergency release.",
            field="emergency_reason",
        )
    if not acknowledge_uncrossmatched:
        raise ServiceError(
            "EMERGENCY_ACKNOWLEDGEMENT_REQUIRED",
            "Confirm that this unit is being released before crossmatch completion.",
        )

    issue = UnitIssue(
        blood_unit_id=unit.id,
        request_id=request.id,
        facility_id=request.facility_id,
        issued_at=now,
        issued_by=actor.display_name,
        collected_by=collected_by,
        destination_ward=_text(destination_ward, maximum=120) or request.ward,
        release_mode="EMERGENCY_UNCROSSMATCHED",
        emergency_release_reason=emergency_reason,
        emergency_authorized_by=authorized_by,
        disposition="AWAITING_OUTCOME",
    )
    unit_before = unit.status
    request_before = snapshot(request, ("units_issued", "status"))

    with audited(db, actor, "EMERGENCY_UNIT_ISSUED", "unit_issue") as entry:
        db.add(issue)
        unit.status = "ISSUED"
        unit.issued_at = now
        request.units_issued += 1
        request.override_reason = emergency_reason
        request.override_by = authorized_by

        if request.patient_blood_group_id is not None and donor_group is not None:
            recipient = db.get(BloodGroup, request.patient_blood_group_id)
            request.was_substituted = bool(
                recipient is not None and recipient.code != donor_group.code
            )

        db.flush()
        _sync_status(db, request, now=now)
        entry.on(issue, after=snapshot(issue, ISSUE_FIELDS))
        entry.note(
            request_before=request_before,
            request_after=snapshot(request, ("units_issued", "status")),
            unit_status_from=unit_before,
            unit_status_to=unit.status,
            recipient_group_known=request.patient_blood_group_id is not None,
            compatibility_rank=getattr(compatibility, "preference_rank", None),
            policy_acknowledged=True,
        )

    return issue


def record_return(
    db: Session,
    actor: Actor,
    *,
    issue_id: str,
    minutes_out_of_storage: int,
    cold_chain_intact: bool,
    reason: str,
    now: datetime = DEMO_DATETIME,
) -> UnitIssue:
    """Return an untransfused unit, accepting it only when policy permits."""

    require(actor, Permission.ISSUE_UNIT, "receive returned blood units")
    issue = _own_issue(db, actor, issue_id)
    request = _own_request(db, actor, issue.request_id)
    unit = _own_unit(db, actor, issue.blood_unit_id)

    if issue.custody_closed_at is not None:
        raise ServiceError(
            "CUSTODY_ALREADY_CLOSED", "A final custody outcome is already recorded."
        )

    transfusion = db.scalars(
        select(TransfusionRecord).where(TransfusionRecord.issue_id == issue.id)
    ).first()

    if transfusion is not None:
        raise ServiceError(
            "TRANSFUSION_RECORDED",
            "A transfused unit cannot be returned to inventory.",
        )

    if unit.status != "ISSUED":
        raise ServiceError(
            "UNIT_NOT_ISSUED",
            "Only an issued unit can be recorded as returned.",
        )

    if not 0 <= minutes_out_of_storage <= 24 * 60:
        raise ServiceError(
            "RETURN_TIME_INVALID",
            "Minutes out of storage must be between 0 and 1,440.",
        )

    reason = _text(reason, maximum=60)

    if len(reason) < 3:
        raise ServiceError(
            "RETURN_REASON_REQUIRED",
            "Record why the unit was returned.",
            field="reason",
        )

    maximum_minutes = int(
        config_get(
            "clinical_operations.return_to_stock.max_minutes_out_of_controlled_storage",
            30,
        )
    )
    accepted = cold_chain_intact and minutes_out_of_storage <= maximum_minutes
    issue_before = snapshot(issue, ISSUE_FIELDS)
    unit_before = unit.status
    request_before = snapshot(request, ("units_issued", "status"))

    with audited(db, actor, "UNIT_RETURNED", "unit_issue", issue.id) as entry:
        issue.returned_at = now
        issue.return_accepted = accepted
        issue.return_reason = reason
        issue.minutes_out_of_storage = minutes_out_of_storage
        issue.disposition = "RETURNED_TO_STOCK" if accepted else "RETURN_REJECTED"
        issue.custody_closed_at = now
        issue.custody_notes = reason

        request.units_issued = max(0, request.units_issued - 1)

        if accepted:
            unit.status = "AVAILABLE"
        else:
            unit.status = "DISCARDED"
            unit.discarded_at = now
            unit.discard_reason = str(
                config_get(
                    "clinical_operations.return_to_stock.rejected_discard_reason",
                    "BROKEN_COLD_CHAIN",
                )
            )

        db.flush()
        _sync_status(db, request, now=now)
        entry.on(
            issue,
            before=issue_before,
            after=snapshot(issue, ISSUE_FIELDS),
        )
        entry.note(
            cold_chain_intact=cold_chain_intact,
            accepted_to_stock=accepted,
            maximum_minutes=maximum_minutes,
            unit_status_from=unit_before,
            unit_status_to=unit.status,
            request_before=request_before,
            request_after=snapshot(request, ("units_issued", "status")),
        )

    return issue


def record_not_returned(
    db: Session,
    actor: Actor,
    *,
    issue_id: str,
    reason: str,
    incident_reference: str,
    now: datetime = DEMO_DATETIME,
) -> UnitIssue:
    """Close custody when a unit is neither returned nor confirmed transfused."""

    require(actor, Permission.ISSUE_UNIT, "record units not returned")
    issue = _own_issue(db, actor, issue_id)
    request = _own_request(db, actor, issue.request_id)
    unit = _own_unit(db, actor, issue.blood_unit_id)

    if issue.custody_closed_at is not None or issue.returned_at is not None:
        raise ServiceError(
            "CUSTODY_ALREADY_CLOSED", "A final custody outcome is already recorded."
        )
    if db.scalars(
        select(TransfusionRecord).where(TransfusionRecord.issue_id == issue.id)
    ).first():
        raise ServiceError(
            "TRANSFUSION_RECORDED",
            "A transfusion outcome is already recorded for this issue.",
        )
    if unit.status != "ISSUED":
        raise ServiceError(
            "UNIT_NOT_ISSUED", "Only an issued unit can be marked not returned."
        )

    reason = _text(reason)
    incident_reference = _text(incident_reference, maximum=120)
    if len(reason) < 12:
        raise ServiceError(
            "NOT_RETURNED_REASON_REQUIRED",
            "Record the custody investigation and why the unit was not returned.",
            field="reason",
        )
    if len(incident_reference) < 3:
        raise ServiceError(
            "INCIDENT_REFERENCE_REQUIRED",
            "Record the incident or investigation reference.",
            field="incident_reference",
        )

    issue_before = snapshot(issue, ISSUE_FIELDS)
    unit_before = unit.status
    request_before = request.status

    with audited(db, actor, "UNIT_NOT_RETURNED", "unit_issue", issue.id) as entry:
        issue.disposition = "NOT_RETURNED"
        issue.custody_closed_at = now
        issue.custody_notes = reason
        unit.status = "DISCARDED"
        unit.discarded_at = now
        unit.discard_reason = "NOT_RETURNED"
        db.flush()

        if (
            request.units_issued >= request.units_requested
            and _unresolved_issue_count(db, request.id) == 0
        ):
            request.status = "CLOSED"
            request.closed_at = now
        else:
            _sync_status(db, request, now=now)

        _sync_demand_event(db, request, now=now)

        entry.on(issue, before=issue_before, after=snapshot(issue, ISSUE_FIELDS))
        entry.note(
            incident_reference=incident_reference,
            reason=reason,
            unit_status_from=unit_before,
            unit_status_to=unit.status,
            request_status_from=request_before,
            request_status_to=request.status,
        )

    return issue


def record_transfusion(
    db: Session,
    actor: Actor,
    *,
    issue_id: str,
    outcome: str,
    reaction_type: str = "NONE",
    reaction_severity: str | None = None,
    reaction_notes: str | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    now: datetime = DEMO_DATETIME,
) -> TransfusionRecord:
    """Record the patient-side disposition of an issued unit."""

    require(actor, Permission.RECORD_TRANSFUSION, "record transfusion outcomes")
    issue = _own_issue(db, actor, issue_id)
    request = _own_request(db, actor, issue.request_id)
    unit = _own_unit(db, actor, issue.blood_unit_id)

    if issue.custody_closed_at is not None or issue.returned_at is not None:
        raise ServiceError(
            "CUSTODY_ALREADY_CLOSED",
            "A final custody outcome is already recorded for this issue.",
        )

    if db.scalars(
        select(TransfusionRecord).where(TransfusionRecord.issue_id == issue.id)
    ).first():
        raise ServiceError(
            "TRANSFUSION_RECORDED",
            "A transfusion outcome is already recorded for this issue.",
        )

    if unit.status != "ISSUED":
        raise ServiceError(
            "UNIT_NOT_ISSUED",
            "Only an issued unit can be recorded as transfused.",
        )

    outcome = _text(outcome).upper()
    reaction_type = _text(reaction_type).upper() or "NONE"
    reaction_severity = _text(reaction_severity).upper() or None
    reaction_notes = _text(reaction_notes)

    if outcome not in TRANSFUSION_OUTCOMES:
        raise ServiceError("OUTCOME_INVALID", "Select a valid transfusion outcome.")

    if reaction_type not in REACTION_TYPES:
        raise ServiceError("REACTION_INVALID", "Select a valid reaction type.")

    if reaction_type == "NONE":
        reaction_severity = None
        reaction_notes = ""
    else:
        if reaction_severity not in REACTION_SEVERITIES:
            raise ServiceError(
                "REACTION_SEVERITY_REQUIRED",
                "Record the severity of the transfusion reaction.",
            )

        if len(reaction_notes) < 12:
            raise ServiceError(
                "REACTION_NOTES_REQUIRED",
                "Record the observed reaction and immediate response.",
                field="reaction_notes",
            )

    started_at = as_utc(started_at) or now
    completed_at = as_utc(completed_at) or now

    if completed_at < started_at:
        raise ServiceError(
            "TRANSFUSION_TIME_INVALID",
            "Completion cannot be earlier than the transfusion start.",
        )

    record = TransfusionRecord(
        blood_unit_id=unit.id,
        request_id=request.id,
        issue_id=issue.id,
        started_at=started_at,
        completed_at=completed_at,
        outcome=outcome,
        reaction_type=reaction_type,
        reaction_severity=reaction_severity,
        reaction_notes=reaction_notes or None,
        reaction_reported_at=now if reaction_type != "NONE" else None,
        recorded_by=actor.display_name,
    )
    unit_before = unit.status
    request_before = request.status

    with audited(db, actor, "TRANSFUSION_RECORDED", "transfusion_record") as entry:
        db.add(record)
        unit.status = "TRANSFUSED"
        issue.disposition = "TRANSFUSED"
        issue.custody_closed_at = completed_at
        issue.custody_notes = reaction_notes or None
        db.flush()

        unresolved = _unresolved_issue_count(db, request.id)

        if request.units_issued >= request.units_requested and unresolved == 0:
            request.status = "CLOSED"
            request.closed_at = now
        else:
            _sync_status(db, request, now=now)

        _sync_demand_event(db, request, now=now)

        entry.on(record, after=snapshot(record, TRANSFUSION_FIELDS))
        entry.note(
            unit_status_from=unit_before,
            unit_status_to=unit.status,
            request_status_from=request_before,
            request_status_to=request.status,
            reaction_recorded=reaction_type != "NONE",
            issue_disposition=issue.disposition,
        )

    return record


def record_replacement_receipt(
    db: Session,
    actor: Actor,
    *,
    request_id: str,
    units_received: int,
    source_reference: str,
) -> BloodRequest:
    """Add a verified replacement receipt to the request's audited ledger."""

    require(actor, Permission.MANAGE_CLINICAL_REQUEST, "record replacement receipts")
    request = _own_request(db, actor, request_id)

    if request.status == "CANCELLED":
        raise ServiceError(
            "REQUEST_CANCELLED", "Replacement cannot be posted to a cancelled request."
        )
    if request.replacement_waived:
        raise ServiceError(
            "REPLACEMENT_WAIVED", "This replacement requirement has been waived."
        )
    if units_received < 1:
        raise ServiceError(
            "REPLACEMENT_RECEIPT_INVALID", "Record at least one replacement unit."
        )

    outstanding = max(
        0, request.replacement_units_required - request.replacement_units_received
    )
    if units_received > outstanding:
        raise ServiceError(
            "REPLACEMENT_RECEIPT_EXCEEDS_REQUIREMENT",
            "The receipt exceeds the outstanding replacement requirement.",
        )

    source_reference = _text(source_reference, maximum=120)
    if len(source_reference) < 3:
        raise ServiceError(
            "REPLACEMENT_REFERENCE_REQUIRED",
            "Record the donation, receipt, or batch reference.",
            field="source_reference",
        )

    before = snapshot(request, REQUEST_FIELDS)
    with audited(
        db,
        actor,
        "REPLACEMENT_RECEIPT_RECORDED",
        "blood_request",
        request.id,
    ) as entry:
        request.replacement_units_received += units_received
        entry.on(request, before=before, after=snapshot(request, REQUEST_FIELDS))
        entry.note(units_received=units_received, source_reference=source_reference)

    return request


def waive_replacement_requirement(
    db: Session,
    actor: Actor,
    *,
    request_id: str,
    reason: str,
) -> BloodRequest:
    """Waive the outstanding replacement requirement with accountable reason."""

    require(actor, Permission.MANAGE_CLINICAL_REQUEST, "waive replacement requirements")
    request = _own_request(db, actor, request_id)

    if request.status == "CANCELLED":
        raise ServiceError(
            "REQUEST_CANCELLED", "A cancelled request has no replacement obligation."
        )
    if request.replacement_waived:
        raise ServiceError(
            "REPLACEMENT_ALREADY_WAIVED", "This requirement is already waived."
        )

    outstanding = max(
        0, request.replacement_units_required - request.replacement_units_received
    )
    if outstanding == 0:
        raise ServiceError(
            "NO_REPLACEMENT_OUTSTANDING", "There is no replacement balance to waive."
        )

    reason = _text(reason, maximum=120)
    if len(reason) < 12:
        raise ServiceError(
            "REPLACEMENT_WAIVER_REASON_REQUIRED",
            "Record why the outstanding replacement requirement is waived.",
            field="reason",
        )

    before = snapshot(request, REQUEST_FIELDS)
    with audited(
        db,
        actor,
        "REPLACEMENT_REQUIREMENT_WAIVED",
        "blood_request",
        request.id,
    ) as entry:
        request.replacement_waived = True
        request.replacement_waived_reason = reason
        entry.on(request, before=before, after=snapshot(request, REQUEST_FIELDS))
        entry.note(outstanding_units=outstanding, reason=reason)

    return request


def cancel_request(
    db: Session,
    actor: Actor,
    *,
    request_id: str,
    reason: str,
    now: datetime = DEMO_DATETIME,
) -> BloodRequest:
    """Cancel an unissued request and release its allocated units."""

    require(actor, Permission.MANAGE_CLINICAL_REQUEST, "cancel clinical requests")
    request = _own_request(db, actor, request_id)
    _assert_open(request)
    reason = _text(reason)

    if len(reason) < 12:
        raise ServiceError(
            "CANCELLATION_REASON_REQUIRED",
            "Record why the clinical request is being cancelled.",
            field="reason",
        )

    if request.units_issued > 0:
        raise ServiceError(
            "ISSUED_REQUEST",
            "A request with issued units cannot be cancelled.",
        )

    allocated_units = list(
        db.scalars(
            select(BloodUnit)
            .join(Crossmatch, Crossmatch.blood_unit_id == BloodUnit.id)
            .where(
                Crossmatch.request_id == request.id,
                Crossmatch.result == "COMPATIBLE",
                BloodUnit.status == "CROSSMATCHED",
            )
        ).all()
    )
    before = snapshot(request, REQUEST_FIELDS)

    with audited(db, actor, "BLOOD_REQUEST_CANCELLED", "blood_request", request.id) as entry:
        for unit in allocated_units:
            unit.status = "AVAILABLE"

        request.status = "CANCELLED"
        request.closed_at = now
        request.notes = f"{request.notes}\n" if request.notes else ""
        request.notes += f"Cancellation: {reason}"
        _sync_demand_event(db, request, now=now)
        entry.on(request, before=before, after=snapshot(request, REQUEST_FIELDS))
        entry.note(reason=reason, units_released=len(allocated_units))

    return request


def close_request(
    db: Session,
    actor: Actor,
    *,
    request_id: str,
    reason: str | None = None,
    now: datetime = DEMO_DATETIME,
) -> BloodRequest:
    """Close a completed or clinically discontinued partial request."""

    require(actor, Permission.MANAGE_CLINICAL_REQUEST, "close clinical requests")
    request = _own_request(db, actor, request_id)
    _assert_open(request)
    reason = _text(reason)

    if request.units_issued < request.units_requested and len(reason) < 12:
        raise ServiceError(
            "PARTIAL_CLOSE_REASON_REQUIRED",
            "Record why the request is closing before all units were fulfilled.",
            field="reason",
        )

    allocated = _active_crossmatch_count(db, request.id, now=now)

    if allocated:
        raise ServiceError(
            "ACTIVE_CROSSMATCHES",
            "Release or issue active crossmatched units before closing the request.",
        )

    unresolved = _unresolved_issue_count(db, request.id)

    if unresolved:
        raise ServiceError(
            "UNRESOLVED_ISSUES",
            "Record the outcome or return of every issued unit before closing.",
        )

    before = snapshot(request, REQUEST_FIELDS)

    with audited(db, actor, "BLOOD_REQUEST_CLOSED", "blood_request", request.id) as entry:
        request.status = "CLOSED"
        request.closed_at = now

        if reason:
            request.notes = f"{request.notes}\n" if request.notes else ""
            request.notes += f"Closure: {reason}"

        _sync_demand_event(db, request, now=now)
        entry.on(request, before=before, after=snapshot(request, REQUEST_FIELDS))
        entry.note(reason=reason or None)

    return request
