"""Clinical requests: the patient-side half of the vein-to-vein chain."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import CurrentUser, Permission, Role, can
from db.models import (
    AuditLog,
    BloodGroup,
    BloodRequest,
    BloodUnit,
    Component,
    Crossmatch,
    TransfusionRecord,
    UnitIssue,
)
from i18n.t import t
from services import request_service
from services.audit import Actor, ServiceError
from services.common import DEMO_DATETIME
from web.deps import Principal, get_db, require_permission, require_principal
from web.routers.facility import ENABLED_NAV, nav_counts
from web.templating import current_lang, flash, render

router = APIRouter(
    prefix="/app/requests",
    dependencies=[Depends(require_permission(Permission.MANAGE_CLINICAL_REQUEST))],
)

CLINICAL_CONTEXTS = (
    "OBSTETRIC",
    "TRAUMA",
    "SURGERY",
    "THALASSAEMIA",
    "ONCOLOGY",
    "MEDICINE",
    "OTHER",
)


def _actor(principal: Principal, request: Request) -> Actor:
    return Actor.from_principal(principal, request)


def _allowed(principal: Principal, permission: Permission) -> bool:
    try:
        role = Role(principal.role)
    except ValueError:
        return False

    return can(
        principal.role_subject(role=role),
        permission,
    )


def _page(request: Request, principal: Principal, db: Session, **kwargs):
    return render(
        request,
        principal=principal,
        db=db,
        nav_counts=nav_counts(db, principal),
        enabled_nav=ENABLED_NAV,
        **kwargs,
    )


def _crumbs(lang: str, *extra: dict) -> list[dict]:
    return [
        {"label": t("nav.dashboard", language=lang), "url": "/app/dashboard"},
        {"label": t("nav.requests", language=lang), "url": "/app/requests"},
        *extra,
    ]


def _optional_int(value: str | None, field: str) -> int | None:
    value = (value or "").strip()

    if not value:
        return None

    try:
        return int(value)
    except ValueError as exc:
        raise ServiceError("NUMBER_INVALID", "Enter a whole number.", field=field) from exc


def _optional_datetime(value: str | None) -> datetime | None:
    value = (value or "").strip()

    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ServiceError(
            "DATETIME_INVALID",
            "Enter a valid required-by date and time.",
            field="required_by",
        ) from exc

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _request_or_none(db: Session, principal: Principal, request_id: str):
    if not principal.facility_id:
        return None

    return db.scalars(
        select(BloodRequest).where(
            BloodRequest.id == request_id,
            BloodRequest.facility_id == principal.facility_id,
        )
    ).first()


@router.get("")
def request_queue(
    request: Request,
    q: str = Query(""),
    status: str = Query("OPEN"),
    urgency: str = Query(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    lang = current_lang(request)
    facility_id = principal.facility_id
    rows = []
    counts = {"open": 0, "emergency": 0, "allocated": 0, "overdue": 0}

    if facility_id:
        base_filter = BloodRequest.facility_id == facility_id
        statement = (
            select(
                BloodRequest.id,
                BloodRequest.request_code,
                BloodRequest.patient_ref,
                BloodRequest.units_requested,
                BloodRequest.units_issued,
                BloodRequest.urgency,
                BloodRequest.clinical_context,
                BloodRequest.ward,
                BloodRequest.requested_at,
                BloodRequest.required_by,
                BloodRequest.status,
                BloodGroup.code.label("group_code"),
                Component.code.label("component_code"),
            )
            .join(Component, Component.id == BloodRequest.component_id)
            .outerjoin(BloodGroup, BloodGroup.id == BloodRequest.patient_blood_group_id)
            .where(base_filter)
        )

        if status == "OPEN":
            statement = statement.where(
                BloodRequest.status.in_(request_service.OPEN_REQUEST_STATUSES)
            )
        elif status:
            statement = statement.where(BloodRequest.status == status)

        if urgency:
            statement = statement.where(BloodRequest.urgency == urgency)

        query = q.strip()

        if query:
            statement = statement.where(
                or_(
                    BloodRequest.request_code.ilike(f"%{query}%"),
                    BloodRequest.patient_ref.ilike(f"%{query}%"),
                    BloodRequest.ward.ilike(f"%{query}%"),
                )
            )

        rows = db.execute(
            statement.order_by(
                BloodRequest.required_by.is_(None),
                BloodRequest.required_by,
                BloodRequest.requested_at.desc(),
            ).limit(250)
        ).all()

        counts = {
            "open": db.scalar(
                select(func.count()).select_from(BloodRequest).where(
                    base_filter,
                    BloodRequest.status.in_(request_service.OPEN_REQUEST_STATUSES),
                )
            )
            or 0,
            "emergency": db.scalar(
                select(func.count()).select_from(BloodRequest).where(
                    base_filter,
                    BloodRequest.status.in_(request_service.OPEN_REQUEST_STATUSES),
                    BloodRequest.urgency.in_(("EMERGENCY", "MASSIVE_TRANSFUSION")),
                )
            )
            or 0,
            "allocated": db.scalar(
                select(func.count()).select_from(BloodRequest).where(
                    base_filter,
                    BloodRequest.status == "CROSSMATCHED",
                )
            )
            or 0,
            "overdue": db.scalar(
                select(func.count()).select_from(BloodRequest).where(
                    base_filter,
                    BloodRequest.status.in_(request_service.OPEN_REQUEST_STATUSES),
                    BloodRequest.required_by.is_not(None),
                    BloodRequest.required_by < DEMO_DATETIME,
                )
            )
            or 0,
        }

    return _page(
        request,
        principal,
        db,
        template="app/requests.html",
        context={
            "requests": rows,
            "counts": counts,
            "filters": {"q": q, "status": status, "urgency": urgency},
            "urgencies": request_service.URGENCIES,
            "can_create": _allowed(principal, Permission.MANAGE_CLINICAL_REQUEST),
            "now": DEMO_DATETIME,
        },
        page_title=t("req.title", language=lang),
        breadcrumbs=_crumbs(lang),
    )


def _new_page(
    request: Request,
    principal: Principal,
    db: Session,
    *,
    form: dict | None = None,
    error: ServiceError | None = None,
    clinical_request: BloodRequest | None = None,
    status_code: int = 200,
):
    lang = current_lang(request)
    editing = clinical_request is not None
    title_key = "req.edit_title" if editing else "req.new_title"
    components = db.execute(
        select(Component.id, Component.code, Component.name_en).order_by(Component.id)
    ).all()
    groups = db.execute(select(BloodGroup.id, BloodGroup.code).order_by(BloodGroup.id)).all()

    return _page(
        request,
        principal,
        db,
        template="app/request_new.html",
        context={
            "components": components,
            "groups": groups,
            "urgencies": request_service.URGENCIES,
            "patient_sexes": request_service.PATIENT_SEXES,
            "clinical_contexts": CLINICAL_CONTEXTS,
            "form": form or {},
            "form_error": error,
            "clinical_request": clinical_request,
            "form_action": (
                f"/app/requests/{clinical_request.id}/edit"
                if clinical_request
                else "/app/requests/new"
            ),
            "cancel_url": (
                f"/app/requests/{clinical_request.id}"
                if clinical_request
                else "/app/requests"
            ),
            "submit_key": "req.save_changes" if editing else "req.create_request",
            "now": DEMO_DATETIME,
        },
        page_title=t(title_key, language=lang),
        breadcrumbs=_crumbs(
            lang,
            {"label": t(title_key, language=lang), "url": None},
        ),
        status_code=status_code,
    )


def _request_form_values(record: BloodRequest) -> dict:
    return {
        "patient_ref": record.patient_ref,
        "patient_age_years": record.patient_age_years,
        "patient_sex": record.patient_sex,
        "patient_blood_group_id": record.patient_blood_group_id,
        "component_id": record.component_id,
        "units_requested": record.units_requested,
        "urgency": record.urgency,
        "clinical_context": record.clinical_context,
        "ward": record.ward,
        "requested_by": record.requested_by,
        "consultant": record.consultant,
        "required_by": (
            record.required_by.strftime("%Y-%m-%dT%H:%M")
            if record.required_by
            else ""
        ),
        "replacement_units_required": record.replacement_units_required,
        "notes": record.notes,
    }


@router.get("/new")
def new_request_form(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    if not _allowed(principal, Permission.MANAGE_CLINICAL_REQUEST):
        flash(request, t("common.not_permitted", language=current_lang(request)), "critical")
        return RedirectResponse("/app/requests", status_code=303)

    return _new_page(request, principal, db)


@router.post("/new")
def create_request(
    request: Request,
    patient_ref: str = Form(...),
    patient_age_years: str = Form(""),
    patient_sex: str = Form("UNKNOWN"),
    patient_blood_group_id: str = Form(""),
    component_id: int = Form(...),
    units_requested: int = Form(...),
    urgency: str = Form(...),
    clinical_context: str = Form("OTHER"),
    ward: str = Form(""),
    requested_by: str = Form(""),
    consultant: str = Form(""),
    required_by: str = Form(""),
    replacement_units_required: int = Form(0),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    form = {
        "patient_ref": patient_ref,
        "patient_age_years": patient_age_years,
        "patient_sex": patient_sex,
        "patient_blood_group_id": patient_blood_group_id,
        "component_id": component_id,
        "units_requested": units_requested,
        "urgency": urgency,
        "clinical_context": clinical_context,
        "ward": ward,
        "requested_by": requested_by,
        "consultant": consultant,
        "required_by": required_by,
        "replacement_units_required": replacement_units_required,
        "notes": notes,
    }

    try:
        record = request_service.create_request(
            db,
            _actor(principal, request),
            patient_ref=patient_ref,
            patient_age_years=_optional_int(patient_age_years, "patient_age_years"),
            patient_sex=patient_sex,
            patient_blood_group_id=_optional_int(
                patient_blood_group_id, "patient_blood_group_id"
            ),
            component_id=component_id,
            units_requested=units_requested,
            urgency=urgency,
            clinical_context=clinical_context,
            ward=ward,
            requested_by=requested_by,
            consultant=consultant,
            required_by=_optional_datetime(required_by),
            replacement_units_required=replacement_units_required,
            notes=notes,
        )
    except ServiceError as error:
        return _new_page(
            request,
            principal,
            db,
            form=form,
            error=error,
            status_code=422,
        )

    flash(request, t("req.created", language=current_lang(request)), "safe")
    return RedirectResponse(f"/app/requests/{record.id}", status_code=303)


@router.get("/{request_id}/edit")
def edit_request_form(
    request: Request,
    request_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    record = _request_or_none(db, principal, request_id)
    if record is None:
        flash(request, t("req.not_found", language=current_lang(request)), "critical")
        return RedirectResponse("/app/requests", status_code=303)
    if not _allowed(principal, Permission.MANAGE_CLINICAL_REQUEST):
        flash(request, t("common.not_permitted", language=current_lang(request)), "critical")
        return RedirectResponse(f"/app/requests/{record.id}", status_code=303)

    return _new_page(
        request,
        principal,
        db,
        form=_request_form_values(record),
        clinical_request=record,
    )


@router.post("/{request_id}/edit")
def edit_request(
    request: Request,
    request_id: str,
    patient_ref: str = Form(...),
    patient_age_years: str = Form(""),
    patient_sex: str = Form("UNKNOWN"),
    patient_blood_group_id: str = Form(""),
    component_id: int = Form(...),
    units_requested: int = Form(...),
    urgency: str = Form(...),
    clinical_context: str = Form("OTHER"),
    ward: str = Form(""),
    requested_by: str = Form(""),
    consultant: str = Form(""),
    required_by: str = Form(""),
    replacement_units_required: int = Form(0),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    record = _request_or_none(db, principal, request_id)
    if record is None:
        flash(request, t("req.not_found", language=current_lang(request)), "critical")
        return RedirectResponse("/app/requests", status_code=303)

    form = {
        "patient_ref": patient_ref,
        "patient_age_years": patient_age_years,
        "patient_sex": patient_sex,
        "patient_blood_group_id": patient_blood_group_id,
        "component_id": component_id,
        "units_requested": units_requested,
        "urgency": urgency,
        "clinical_context": clinical_context,
        "ward": ward,
        "requested_by": requested_by,
        "consultant": consultant,
        "required_by": required_by,
        "replacement_units_required": replacement_units_required,
        "notes": notes,
    }
    try:
        request_service.update_request(
            db,
            _actor(principal, request),
            request_id=request_id,
            patient_ref=patient_ref,
            patient_age_years=_optional_int(patient_age_years, "patient_age_years"),
            patient_sex=patient_sex,
            patient_blood_group_id=_optional_int(
                patient_blood_group_id, "patient_blood_group_id"
            ),
            component_id=component_id,
            units_requested=units_requested,
            urgency=urgency,
            clinical_context=clinical_context,
            ward=ward,
            requested_by=requested_by,
            consultant=consultant,
            required_by=_optional_datetime(required_by),
            replacement_units_required=replacement_units_required,
            notes=notes,
        )
    except ServiceError as error:
        return _new_page(
            request,
            principal,
            db,
            form=form,
            error=error,
            clinical_request=record,
            status_code=422,
        )

    flash(request, t("req.updated", language=current_lang(request)), "safe")
    return RedirectResponse(f"/app/requests/{record.id}", status_code=303)


@router.get("/{request_id}")
def request_detail(
    request: Request,
    request_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    lang = current_lang(request)
    record = _request_or_none(db, principal, request_id)

    if record is None:
        flash(request, t("req.not_found", language=lang), "critical")
        return RedirectResponse("/app/requests", status_code=303)

    component = db.get(Component, record.component_id)
    patient_group = (
        db.get(BloodGroup, record.patient_blood_group_id)
        if record.patient_blood_group_id
        else None
    )

    crossmatch_rows = db.execute(
        select(
            Crossmatch,
            BloodUnit.din,
            BloodUnit.status.label("unit_status"),
            BloodUnit.expires_at,
            BloodGroup.code.label("donor_group_code"),
        )
        .join(BloodUnit, BloodUnit.id == Crossmatch.blood_unit_id)
        .join(BloodGroup, BloodGroup.id == BloodUnit.blood_group_id)
        .where(Crossmatch.request_id == record.id)
        .order_by(Crossmatch.performed_at.desc())
    ).all()
    crossmatches = [
        {
            "record": row.Crossmatch,
            "din": row.din,
            "unit_status": row.unit_status,
            "expires_at": row.expires_at,
            "donor_group_code": row.donor_group_code,
        }
        for row in crossmatch_rows
    ]

    transfusions = {
        item.issue_id: item
        for item in db.scalars(
            select(TransfusionRecord).where(TransfusionRecord.request_id == record.id)
        ).all()
    }
    issue_rows = db.execute(
        select(UnitIssue, BloodUnit.din, BloodGroup.code.label("donor_group_code"))
        .join(BloodUnit, BloodUnit.id == UnitIssue.blood_unit_id)
        .join(BloodGroup, BloodGroup.id == BloodUnit.blood_group_id)
        .where(UnitIssue.request_id == record.id)
        .order_by(UnitIssue.issued_at.desc())
    ).all()
    issues = [
        {
            "record": row.UnitIssue,
            "din": row.din,
            "donor_group_code": row.donor_group_code,
            "transfusion": transfusions.get(row.UnitIssue.id),
        }
        for row in issue_rows
    ]

    related_ids = [record.id]
    related_ids.extend(item["record"].id for item in crossmatches)
    related_ids.extend(item["record"].id for item in issues)
    related_ids.extend(
        item["transfusion"].id for item in issues if item["transfusion"] is not None
    )
    timeline = list(
        db.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id.in_(related_ids))
            .order_by(AuditLog.created_at.desc())
            .limit(100)
        ).all()
    )
    batch_events = db.scalars(
        select(AuditLog)
        .where(
            AuditLog.action == "CROSSMATCHES_EXPIRED",
            AuditLog.entity_id == record.facility_id,
        )
        .order_by(AuditLog.created_at.desc())
        .limit(50)
    ).all()
    for event in batch_events:
        context = (event.after_json or {}).get("_context", {})
        if record.id in context.get("request_ids", []):
            timeline.append(event)
    timeline.sort(key=lambda event: event.created_at, reverse=True)

    candidates = (
        request_service.candidate_units(
            db,
            _actor(principal, request),
            request_id=record.id,
            limit=12,
        )
        if record.status in request_service.OPEN_REQUEST_STATUSES
        else []
    )
    emergency_candidates = (
        request_service.emergency_candidate_units(
            db,
            _actor(principal, request),
            request_id=record.id,
            limit=12,
        )
        if record.status in request_service.OPEN_REQUEST_STATUSES
        else []
    )
    emergency_candidate_ids = {row.id for row in emergency_candidates}
    if not candidates and emergency_candidates:
        candidates = emergency_candidates
    permissions = {
        "manage": _allowed(principal, Permission.MANAGE_CLINICAL_REQUEST),
        "crossmatch": _allowed(principal, Permission.PERFORM_CROSSMATCH),
        "issue": _allowed(principal, Permission.ISSUE_UNIT),
        "transfusion": _allowed(principal, Permission.RECORD_TRANSFUSION),
    }

    return _page(
        request,
        principal,
        db,
        template="app/request_detail.html",
        context={
            "clinical_request": record,
            "component": component,
            "patient_group": patient_group,
            "crossmatches": crossmatches,
            "issues": issues,
            "timeline": timeline,
            "candidates": candidates,
            "emergency_candidate_ids": emergency_candidate_ids,
            "permissions": permissions,
            "crossmatch_methods": request_service.CROSSMATCH_METHODS,
            "reaction_types": request_service.REACTION_TYPES,
            "reaction_severities": request_service.REACTION_SEVERITIES,
            "transfusion_outcomes": request_service.TRANSFUSION_OUTCOMES,
            "now": DEMO_DATETIME,
        },
        page_title=record.request_code,
        breadcrumbs=_crumbs(lang, {"label": record.request_code, "url": None}),
    )


def _action_redirect(request_id: str):
    return RedirectResponse(f"/app/requests/{request_id}", status_code=303)


def _run_action(request: Request, request_id: str, action, success_key: str):
    try:
        action()
    except ServiceError as error:
        flash(request, error.message, "critical")
    else:
        flash(request, t(success_key, language=current_lang(request)), "safe")

    return _action_redirect(request_id)


@router.post("/{request_id}/crossmatch")
def crossmatch(
    request: Request,
    request_id: str,
    unit_id: str = Form(...),
    result: str = Form(...),
    method: str = Form(...),
    notes: str = Form(""),
    override_reason: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    return _run_action(
        request,
        request_id,
        lambda: request_service.record_crossmatch(
            db,
            _actor(principal, request),
            request_id=request_id,
            unit_id=unit_id,
            result=result,
            method=method,
            notes=notes,
            override_reason=override_reason,
        ),
        "req.crossmatch_saved",
    )


@router.post("/{request_id}/crossmatch/{unit_id}/release")
def release_crossmatch(
    request: Request,
    request_id: str,
    unit_id: str,
    reason: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    return _run_action(
        request,
        request_id,
        lambda: request_service.release_crossmatch(
            db,
            _actor(principal, request),
            request_id=request_id,
            unit_id=unit_id,
            reason=reason,
        ),
        "req.crossmatch_released",
    )


@router.post("/{request_id}/issue/{unit_id}")
def issue(
    request: Request,
    request_id: str,
    unit_id: str,
    collected_by: str = Form(...),
    patient_ref_confirmation: str = Form(...),
    destination_ward: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    return _run_action(
        request,
        request_id,
        lambda: request_service.issue_unit(
            db,
            _actor(principal, request),
            request_id=request_id,
            unit_id=unit_id,
            collected_by=collected_by,
            patient_ref_confirmation=patient_ref_confirmation,
            destination_ward=destination_ward,
        ),
        "req.unit_issued",
    )


@router.post("/{request_id}/emergency-issue/{unit_id}")
def emergency_issue(
    request: Request,
    request_id: str,
    unit_id: str,
    collected_by: str = Form(...),
    patient_ref_confirmation: str = Form(...),
    destination_ward: str = Form(""),
    emergency_reason: str = Form(...),
    authorized_by: str = Form(...),
    acknowledge_uncrossmatched: str | None = Form(None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    return _run_action(
        request,
        request_id,
        lambda: request_service.emergency_issue_unit(
            db,
            _actor(principal, request),
            request_id=request_id,
            unit_id=unit_id,
            collected_by=collected_by,
            patient_ref_confirmation=patient_ref_confirmation,
            destination_ward=destination_ward,
            emergency_reason=emergency_reason,
            authorized_by=authorized_by,
            acknowledge_uncrossmatched=acknowledge_uncrossmatched == "yes",
        ),
        "req.emergency_unit_issued",
    )


@router.post("/{request_id}/return/{issue_id}")
def return_unit(
    request: Request,
    request_id: str,
    issue_id: str,
    minutes_out_of_storage: int = Form(...),
    cold_chain_intact: str | None = Form(None),
    reason: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    return _run_action(
        request,
        request_id,
        lambda: request_service.record_return(
            db,
            _actor(principal, request),
            issue_id=issue_id,
            minutes_out_of_storage=minutes_out_of_storage,
            cold_chain_intact=cold_chain_intact == "yes",
            reason=reason,
        ),
        "req.return_saved",
    )


@router.post("/{request_id}/not-returned/{issue_id}")
def not_returned(
    request: Request,
    request_id: str,
    issue_id: str,
    reason: str = Form(...),
    incident_reference: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    return _run_action(
        request,
        request_id,
        lambda: request_service.record_not_returned(
            db,
            _actor(principal, request),
            issue_id=issue_id,
            reason=reason,
            incident_reference=incident_reference,
        ),
        "req.not_returned_saved",
    )


@router.post("/{request_id}/transfusion/{issue_id}")
def transfusion(
    request: Request,
    request_id: str,
    issue_id: str,
    outcome: str = Form(...),
    reaction_type: str = Form("NONE"),
    reaction_severity: str = Form(""),
    reaction_notes: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    return _run_action(
        request,
        request_id,
        lambda: request_service.record_transfusion(
            db,
            _actor(principal, request),
            issue_id=issue_id,
            outcome=outcome,
            reaction_type=reaction_type,
            reaction_severity=reaction_severity,
            reaction_notes=reaction_notes,
        ),
        "req.transfusion_saved",
    )


@router.post("/{request_id}/replacement/receipt")
def replacement_receipt(
    request: Request,
    request_id: str,
    units_received: int = Form(...),
    source_reference: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    return _run_action(
        request,
        request_id,
        lambda: request_service.record_replacement_receipt(
            db,
            _actor(principal, request),
            request_id=request_id,
            units_received=units_received,
            source_reference=source_reference,
        ),
        "req.replacement_receipt_saved",
    )


@router.post("/{request_id}/replacement/waive")
def replacement_waiver(
    request: Request,
    request_id: str,
    reason: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    return _run_action(
        request,
        request_id,
        lambda: request_service.waive_replacement_requirement(
            db,
            _actor(principal, request),
            request_id=request_id,
            reason=reason,
        ),
        "req.replacement_waived_saved",
    )


@router.post("/{request_id}/cancel")
def cancel(
    request: Request,
    request_id: str,
    reason: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    return _run_action(
        request,
        request_id,
        lambda: request_service.cancel_request(
            db,
            _actor(principal, request),
            request_id=request_id,
            reason=reason,
        ),
        "req.cancelled",
    )


@router.post("/{request_id}/close")
def close(
    request: Request,
    request_id: str,
    reason: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    return _run_action(
        request,
        request_id,
        lambda: request_service.close_request(
            db,
            _actor(principal, request),
            request_id=request_id,
            reason=reason,
        ),
        "req.closed",
    )
