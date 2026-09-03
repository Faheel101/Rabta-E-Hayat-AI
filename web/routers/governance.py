"""Facilities, impact analytics and governed platform administration."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import Permission, ROLE_LABEL_KEYS, Role, can_open_page
from db.models import (
    AuditLog,
    BloodGroup,
    Component,
    Facility,
    ForecastRunSummary,
    MartDaysOfCover,
    MartFacilityKpi,
    MartImpact,
    Organization,
    StorageLocation,
    UserAccount,
)
from i18n.t import t
from services import governance_service, network_onboarding, release_acceptance
from services.audit import Actor, ServiceError, audited
from services.feed_health_service import rows as feed_rows
from web.deps import Principal, get_db, principal_can, require_principal
from web.routers.facility import ENABLED_NAV, nav_counts
from web.templating import current_lang, flash, render

router = APIRouter()


def _guard(principal: Principal, page_key: str) -> None:
    if not can_open_page(principal.role_subject(), page_key):
        raise HTTPException(status_code=403, detail="This workspace is not available to your role")


def _page(request: Request, principal: Principal, db: Session, **kwargs):
    return render(
        request,
        principal=principal,
        db=db,
        nav_counts=nav_counts(db, principal),
        enabled_nav=ENABLED_NAV,
        **kwargs,
    )


def _require_network_management(principal: Principal) -> None:
    if not principal_can(principal, Permission.MANAGE_NETWORK):
        raise HTTPException(status_code=403, detail="Network onboarding is not permitted")


def _service_message(error: ServiceError, language: str) -> str:
    key = f"governance.error_{error.code.lower()}"
    translated = t(key, language=language)
    return error.message if translated.startswith("[") else translated


def _onboarding_options(db: Session) -> dict:
    organizations = list(
        db.scalars(
            select(Organization)
            .where(Organization.is_active.is_(True))
            .order_by(Organization.name_en)
        ).all()
    )
    parent_rbcs = list(
        db.scalars(
            select(Facility)
            .where(Facility.is_active.is_(True), Facility.facility_type == "RBC")
            .order_by(Facility.province, Facility.name_en)
        ).all()
    )
    return {"organizations": organizations, "parent_rbcs": parent_rbcs}


def _onboarding_defaults() -> dict:
    """Safe, useful first-render values for the guided setup form."""

    return {
        "organization_action": "NEW",
        "operating_mode": "STANDALONE",
        "province": "Punjab",
        "facility_type": "DHQ",
        "bed_count": 200,
        "latitude": 31.5204,
        "longitude": 74.3587,
        "integration_mode": "SIMULATED",
        "network_response_sla_minutes": 60,
        "fridge_capacity": 250,
        "freezer_capacity": 150,
        "platelet_capacity": 48,
        "has_obgyn": True,
        "admin_role": Role.BLOOD_BANK_OFFICER.value,
    }


@router.get("/admin/onboarding")
def onboarding_workspace(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal, "admin")
    _require_network_management(principal)
    drafts = network_onboarding.list_drafts(db, Actor.from_principal(principal, request))
    return _page(
        request,
        principal,
        db,
        template="governance/onboarding.html",
        context={"drafts": drafts},
        page_title=t("governance.onboarding_title", language=current_lang(request)),
        breadcrumbs=[
            {"label": t("nav.admin", language=current_lang(request)), "url": "/admin"},
            {"label": t("governance.onboarding_title", language=current_lang(request)), "url": None},
        ],
    )


@router.get("/admin/onboarding/new")
def onboarding_new(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal, "admin")
    _require_network_management(principal)
    return _page(
        request,
        principal,
        db,
        template="governance/onboarding_new.html",
        context={**_onboarding_options(db), "values": _onboarding_defaults()},
        page_title=t("governance.onboarding_new", language=current_lang(request)),
        breadcrumbs=[
            {"label": t("governance.onboarding_title", language=current_lang(request)), "url": "/admin/onboarding"},
            {"label": t("governance.onboarding_new", language=current_lang(request)), "url": None},
        ],
    )


@router.post("/admin/onboarding")
def onboarding_create(
    request: Request,
    organization_action: str = Form("NEW"),
    operating_mode: str = Form("STANDALONE"),
    existing_organization_id: str = Form(""),
    organization_code: str = Form(""),
    organization_name_en: str = Form(""),
    organization_name_ur: str = Form(""),
    province: str = Form("Punjab"),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    facility_code: str = Form(...),
    facility_name_en: str = Form(...),
    facility_name_ur: str = Form(""),
    facility_type: str = Form(...),
    district: str = Form(...),
    division: str = Form(""),
    latitude: float = Form(...),
    longitude: float = Form(...),
    bed_count: int = Form(...),
    parent_rbc_id: str = Form(""),
    integration_mode: str = Form(...),
    shares_inventory: str = Form(""),
    shares_contact: str = Form(""),
    network_response_sla_minutes: int = Form(...),
    has_trauma_centre: str = Form(""),
    has_obgyn: str = Form(""),
    has_oncology: str = Form(""),
    has_thalassaemia_centre: str = Form(""),
    has_cardiac_surgery: str = Form(""),
    fridge_capacity: int = Form(...),
    freezer_capacity: int = Form(0),
    platelet_capacity: int = Form(0),
    admin_name: str = Form(...),
    admin_email: str = Form(...),
    admin_role: str = Form(...),
    temporary_password: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _require_network_management(principal)
    values = {
        "organization_action": organization_action,
        "operating_mode": operating_mode,
        "existing_organization_id": existing_organization_id,
        "organization_code": organization_code,
        "organization_name_en": organization_name_en,
        "organization_name_ur": organization_name_ur,
        "province": province,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "facility_code": facility_code,
        "facility_name_en": facility_name_en,
        "facility_name_ur": facility_name_ur,
        "facility_type": facility_type,
        "district": district,
        "division": division,
        "latitude": latitude,
        "longitude": longitude,
        "bed_count": bed_count,
        "parent_rbc_id": parent_rbc_id,
        "integration_mode": integration_mode,
        "shares_inventory": shares_inventory == "yes",
        "shares_contact": shares_contact == "yes",
        "network_response_sla_minutes": network_response_sla_minutes,
        "has_trauma_centre": has_trauma_centre == "yes",
        "has_obgyn": has_obgyn == "yes",
        "has_oncology": has_oncology == "yes",
        "has_thalassaemia_centre": has_thalassaemia_centre == "yes",
        "has_cardiac_surgery": has_cardiac_surgery == "yes",
        "fridge_capacity": fridge_capacity,
        "freezer_capacity": freezer_capacity,
        "platelet_capacity": platelet_capacity,
        "admin_name": admin_name,
        "admin_email": admin_email,
        "admin_role": admin_role,
    }
    try:
        facility = network_onboarding.create_draft(
            db,
            Actor.from_principal(principal, request),
            organization_action=organization_action,
            operating_mode=operating_mode,
            existing_organization_id=existing_organization_id or None,
            organization_code=organization_code,
            organization_name_en=organization_name_en,
            organization_name_ur=organization_name_ur,
            province=province,
            contact_email=contact_email,
            contact_phone=contact_phone,
            facility_code=facility_code,
            facility_name_en=facility_name_en,
            facility_name_ur=facility_name_ur,
            facility_type=facility_type,
            district=district,
            division=division,
            latitude=latitude,
            longitude=longitude,
            bed_count=bed_count,
            parent_rbc_id=parent_rbc_id or None,
            integration_mode=integration_mode,
            shares_inventory=shares_inventory == "yes",
            shares_contact=shares_contact == "yes",
            network_response_sla_minutes=network_response_sla_minutes,
            has_trauma_centre=has_trauma_centre == "yes",
            has_obgyn=has_obgyn == "yes",
            has_oncology=has_oncology == "yes",
            has_thalassaemia_centre=has_thalassaemia_centre == "yes",
            has_cardiac_surgery=has_cardiac_surgery == "yes",
            fridge_capacity=fridge_capacity,
            freezer_capacity=freezer_capacity,
            platelet_capacity=platelet_capacity,
            admin_name=admin_name,
            admin_email=admin_email,
            admin_role=admin_role,
            temporary_password=temporary_password,
        )
    except ServiceError as error:
        return _page(
            request,
            principal,
            db,
            template="governance/onboarding_new.html",
            context={
                **_onboarding_options(db),
                "values": values,
                "form_error": _service_message(error, current_lang(request)),
                "error_field": error.field,
            },
            page_title=t("governance.onboarding_new", language=current_lang(request)),
            status_code=422,
        )
    flash(request, t("governance.onboarding_draft_created", language=current_lang(request)), "safe")
    return RedirectResponse(f"/admin/onboarding/{facility.id}", status_code=303)


@router.get("/admin/onboarding/{facility_id}")
def onboarding_detail(
    request: Request,
    facility_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal, "admin")
    _require_network_management(principal)
    try:
        state = network_onboarding.get_draft(
            db, Actor.from_principal(principal, request), facility_id
        )
    except ServiceError as error:
        raise HTTPException(status_code=404, detail=error.message) from error
    return _page(
        request,
        principal,
        db,
        template="governance/onboarding_detail.html",
        context=state,
        page_title=state["facility"].name_en,
        breadcrumbs=[
            {"label": t("governance.onboarding_title", language=current_lang(request)), "url": "/admin/onboarding"},
            {"label": state["facility"].name_en, "url": None},
        ],
    )


@router.post("/admin/onboarding/{facility_id}/storage")
def onboarding_add_storage(
    request: Request,
    facility_id: str,
    code: str = Form(...),
    name: str = Form(...),
    location_type: str = Form(...),
    target_temp_min_c: float = Form(...),
    target_temp_max_c: float = Form(...),
    capacity_units: int = Form(...),
    is_quarantine: str = Form(""),
    has_agitator: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    try:
        network_onboarding.add_storage_location(
            db,
            Actor.from_principal(principal, request),
            facility_id,
            code=code,
            name=name,
            location_type=location_type,
            target_temp_min_c=target_temp_min_c,
            target_temp_max_c=target_temp_max_c,
            capacity_units=capacity_units,
            is_quarantine=is_quarantine == "yes",
            has_agitator=has_agitator == "yes",
        )
    except ServiceError as error:
        flash(request, _service_message(error, current_lang(request)), "critical")
    else:
        flash(request, t("governance.storage_added", language=current_lang(request)), "safe")
    return RedirectResponse(f"/admin/onboarding/{facility_id}#storage", status_code=303)


@router.post("/admin/onboarding/{facility_id}/activate")
def onboarding_activate(
    request: Request,
    facility_id: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    try:
        facility = network_onboarding.activate(
            db, Actor.from_principal(principal, request), facility_id
        )
    except ServiceError as error:
        flash(request, _service_message(error, current_lang(request)), "critical")
        return RedirectResponse(f"/admin/onboarding/{facility_id}", status_code=303)
    from services.intelligence_refresh import run_pending

    background.add_task(
        run_pending,
        requested_by=f"{principal.display_name} <{principal.user_id}>",
    )
    flash(
        request,
        t("governance.onboarding_activated", language=current_lang(request), facility=facility.name_en),
        "safe",
    )
    return RedirectResponse(f"/insights/facilities/{facility.id}", status_code=303)


def _facility_rows(db: Session, principal: Principal) -> list[dict]:
    facilities = principal.scope_facilities
    ids = [facility.id for facility in facilities]
    kpis = {
        row.facility_id: row
        for row in db.scalars(
            select(MartFacilityKpi).where(MartFacilityKpi.facility_id.in_(ids or ["__none__"]))
        ).all()
    }
    feeds = {row["facility"].id: row for row in feed_rows(db, facilities)}
    return [
        {"facility": facility, "kpi": kpis.get(facility.id), "feed": feeds.get(facility.id)}
        for facility in facilities
    ]


@router.get("/insights/facilities")
def facilities(
    request: Request,
    q: str = Query(""),
    status_filter: str = Query("", alias="status"),
    facility_type: str = Query("", alias="type"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal, "facilities")
    all_rows = _facility_rows(db, principal)
    rows = list(all_rows)
    needle = q.strip().lower()
    if needle:
        rows = [
            row
            for row in rows
            if needle in row["facility"].name_en.lower()
            or needle in row["facility"].code.lower()
            or needle in row["facility"].district.lower()
        ]
    if status_filter == "degraded":
        rows = [row for row in rows if row["feed"] and row["feed"]["status"] != "HEALTHY"]
    elif status_filter == "critical":
        rows = [row for row in rows if row["kpi"] and row["kpi"].critical_series]
    if facility_type:
        rows = [row for row in rows if row["facility"].facility_type == facility_type]

    summary = {
        "total": len(all_rows),
        "healthy": sum(1 for row in all_rows if row["feed"] and row["feed"]["status"] == "HEALTHY"),
        "critical": sum(1 for row in all_rows if row["kpi"] and row["kpi"].critical_series),
        "units": sum(int(row["kpi"].units_available or 0) for row in all_rows if row["kpi"]),
    }
    latitudes = [row["facility"].latitude for row in all_rows]
    longitudes = [row["facility"].longitude for row in all_rows]
    lat_span = (max(latitudes) - min(latitudes)) if latitudes else 1
    lon_span = (max(longitudes) - min(longitudes)) if longitudes else 1
    map_points = []
    for row in all_rows:
        facility = row["facility"]
        map_points.append(
            {
                **row,
                "x": 5 + ((facility.longitude - min(longitudes)) / (lon_span or 1)) * 90,
                "y": 8 + ((max(latitudes) - facility.latitude) / (lat_span or 1)) * 84,
            }
        )
    lang = current_lang(request)
    return _page(
        request,
        principal,
        db,
        template="governance/facilities.html",
        context={
            "rows": rows,
            "summary": summary,
            "filters": {"q": q, "status": status_filter, "type": facility_type},
            "facility_types": sorted({row["facility"].facility_type for row in all_rows}),
            "map_points": map_points,
        },
        page_title=t("nav.facilities", language=lang),
    )


@router.get("/insights/facilities/{facility_id}")
def facility_detail(
    request: Request,
    facility_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal, "facilities")
    principal.require_scope_facility(facility_id)
    facility = db.get(Facility, facility_id)
    kpi = db.scalar(select(MartFacilityKpi).where(MartFacilityKpi.facility_id == facility_id))
    series = [
        {"record": row.MartDaysOfCover, "component_code": row.component_code, "group_code": row.group_code}
        for row in db.execute(
            select(
                MartDaysOfCover,
                Component.code.label("component_code"),
                BloodGroup.code.label("group_code"),
            )
            .join(Component, Component.id == MartDaysOfCover.component_id)
            .join(BloodGroup, BloodGroup.id == MartDaysOfCover.blood_group_id)
            .where(MartDaysOfCover.facility_id == facility_id)
            .order_by(MartDaysOfCover.days_of_cover)
            .limit(12)
        ).all()
    ]
    stores = list(
        db.scalars(
            select(StorageLocation)
            .where(StorageLocation.facility_id == facility_id)
            .order_by(StorageLocation.name)
        ).all()
    )
    feed = feed_rows(db, [facility])[0]
    can_edit = principal_can(principal, Permission.EDIT_FACILITY_SETTINGS)
    lang = current_lang(request)
    return _page(
        request,
        principal,
        db,
        template="governance/facility_detail.html",
        context={
            "facility": facility,
            "kpi": kpi,
            "series": series,
            "stores": stores,
            "feed": feed,
            "can_edit": can_edit,
        },
        page_title=facility.name_en,
        breadcrumbs=[
            {"label": t("nav.facilities", language=lang), "url": "/insights/facilities"},
            {"label": facility.name_en, "url": None},
        ],
    )


@router.post("/insights/facilities/{facility_id}/settings")
def save_facility_settings(
    request: Request,
    facility_id: str,
    integration_mode: str = Form(...),
    network_response_sla_minutes: int = Form(...),
    shares_inventory: str = Form(""),
    shares_contact: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    try:
        governance_service.update_facility_settings(
            db,
            Actor.from_principal(principal, request),
            facility_id,
            integration_mode=integration_mode,
            network_response_sla_minutes=network_response_sla_minutes,
            shares_inventory=shares_inventory == "yes",
            shares_contact=shares_contact == "yes",
        )
    except ServiceError as exc:
        flash(request, exc.message, "critical")
    else:
        flash(request, t("governance.facility_saved", language=current_lang(request)), "safe")
    return RedirectResponse(f"/insights/facilities/{facility_id}", status_code=303)


def _analytics_payload(db: Session, principal: Principal) -> dict:
    facility_ids = principal.scope_facility_ids
    facilities = list(
        db.scalars(
            select(MartFacilityKpi)
            .where(MartFacilityKpi.facility_id.in_(facility_ids or ["__none__"]))
            .order_by(MartFacilityKpi.wastage_pct_30d.desc())
        ).all()
    )
    impact = list(
        reversed(
            list(db.scalars(select(MartImpact).order_by(MartImpact.impact_date.desc()).limit(30)).all())
        )
    )
    quality = db.scalar(select(ForecastRunSummary).order_by(ForecastRunSummary.generated_at.desc()))
    fill_values = [row.fill_rate_30d for row in facilities if row.fill_rate_30d is not None]
    waste_values = [row.wastage_pct_30d for row in facilities if row.wastage_pct_30d is not None]
    summary = {
        "facilities": len(facilities),
        "available_units": sum(int(row.units_available or 0) for row in facilities),
        "at_risk": sum(int(row.units_at_risk or 0) for row in facilities),
        "fill_rate": sum(fill_values) / len(fill_values) if fill_values else None,
        "wastage": sum(waste_values) / len(waste_values) if waste_values else None,
    }
    return {"facilities": facilities, "impact": impact, "quality": quality, "summary": summary}


@router.get("/insights/analytics")
def analytics(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal, "analytics")
    return _page(
        request,
        principal,
        db,
        template="governance/analytics.html",
        context=_analytics_payload(db, principal),
        page_title=t("nav.analytics", language=current_lang(request)),
    )


@router.get("/insights/analytics/export.csv")
def analytics_export(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal, "analytics")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["facility", "district", "units_available", "critical_series", "units_at_risk", "fill_rate_30d", "wastage_pct_30d"])
    for row in _analytics_payload(db, principal)["facilities"]:
        writer.writerow([row.name_en, row.district, row.units_available, row.critical_series, row.units_at_risk, row.fill_rate_30d, row.wastage_pct_30d])
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=rabta-impact.csv"},
    )


def _admin_users(db: Session, principal: Principal):
    statement = (
        select(UserAccount, Organization.name_en.label("organization_name"), Facility.name_en.label("facility_name"))
        .join(Organization, Organization.id == UserAccount.organization_id)
        .outerjoin(Facility, Facility.id == UserAccount.facility_id)
        .order_by(Organization.name_en, UserAccount.full_name)
    )
    if principal.role != Role.SYSTEM_ADMIN.value:
        statement = statement.where(UserAccount.organization_id == principal.organization_id)
    return list(db.execute(statement).all())


def _user_management_options(db: Session, principal: Principal) -> tuple[list, list]:
    organizations = list(
        db.scalars(
            select(Organization)
            .where(Organization.is_active.is_(True))
            .order_by(Organization.name_en)
        ).all()
    )
    if principal.role != Role.SYSTEM_ADMIN.value:
        organizations = [item for item in organizations if item.id == principal.organization_id]
    organization_ids = [item.id for item in organizations]
    facilities = list(
        db.scalars(
            select(Facility)
            .where(
                Facility.organization_id.in_(organization_ids or ["__none__"]),
                Facility.is_active.is_(True),
            )
            .order_by(Facility.name_en)
        ).all()
    )
    return organizations, facilities


@router.get("/admin/release")
def release_workspace(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    """Live release dossier; platform configuration, never a clinical control."""

    _guard(principal, "admin")
    _require_network_management(principal)
    return _page(
        request,
        principal,
        db,
        template="governance/release.html",
        context=release_acceptance.acceptance_snapshot(db),
        page_title=t("release.title", language=current_lang(request)),
        breadcrumbs=[
            {"label": t("nav.admin", language=current_lang(request)), "url": "/admin"},
            {"label": t("release.title", language=current_lang(request)), "url": None},
        ],
    )


@router.get("/admin")
def admin(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal, "admin")
    users = _admin_users(db, principal)
    audit_statement = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(100)
    if principal.role != Role.SYSTEM_ADMIN.value:
        identities = [row.UserAccount.id for row in users]
        emails = [row.UserAccount.email for row in users]
        audit_statement = (
            select(AuditLog)
            .where(
                or_(
                    AuditLog.actor.in_(emails),
                    *[AuditLog.actor.like(f"%<{user_id}>%") for user_id in identities],
                )
            )
            .order_by(AuditLog.created_at.desc())
            .limit(100)
        )
    audits = list(db.scalars(audit_statement).all())
    roles = [
        {"value": role.value, "label": t(ROLE_LABEL_KEYS[role], language=current_lang(request))}
        for role in Role
        if role is not Role.SYSTEM_ADMIN or principal.role == Role.SYSTEM_ADMIN.value
    ]
    organizations, user_facilities = _user_management_options(db, principal)
    return _page(
        request,
        principal,
        db,
        template="governance/admin.html",
        context={
            "users": users,
            "roles": roles,
            "organizations": organizations,
            "user_facilities": user_facilities,
            "audits": audits,
            "weights": governance_service.optimizer_weights(db),
            "can_manage_users": principal_can(principal, Permission.MANAGE_USERS),
            "can_manage_network": principal_can(principal, Permission.MANAGE_NETWORK),
            "can_change_weights": principal_can(principal, Permission.CHANGE_OPTIMIZER_WEIGHTS),
            "can_run_optimizer": principal_can(principal, Permission.RUN_OPTIMIZER),
            "can_refresh_intelligence": principal_can(
                principal, Permission.RUN_OPTIMIZER
            ),
        },
        page_title=t("nav.admin", language=current_lang(request)),
    )


@router.post("/admin/optimizer-weights")
def save_optimizer_weights(
    request: Request,
    shortage: float = Form(...),
    waste: float = Form(...),
    transport: float = Form(...),
    fixed_dispatch: float = Form(...),
    substitution: float = Form(...),
    capacity: float = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    try:
        governance_service.update_optimizer_weights(
            db,
            Actor.from_principal(principal, request),
            {
                "shortage": shortage,
                "waste": waste,
                "transport": transport,
                "fixed_dispatch": fixed_dispatch,
                "substitution": substitution,
                "capacity": capacity,
            },
        )
    except ServiceError as exc:
        flash(request, exc.message, "critical")
    else:
        flash(request, t("governance.weights_saved", language=current_lang(request)), "safe")
    return RedirectResponse("/admin#optimizer", status_code=303)


@router.post("/admin/users/{user_id}")
def save_user(
    request: Request,
    user_id: str,
    role: str = Form(...),
    is_active: str = Form(""),
    facility_id: str = Form("__UNCHANGED__"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    try:
        governance_service.update_user(
            db,
            Actor.from_principal(principal, request),
            user_id,
            role=role,
            is_active=is_active == "yes",
            **(
                {}
                if facility_id == "__UNCHANGED__"
                else {"facility_id": facility_id or None}
            ),
        )
    except ServiceError as exc:
        flash(request, exc.message, "critical")
    else:
        flash(request, t("governance.user_saved", language=current_lang(request)), "safe")
    return RedirectResponse("/admin#users", status_code=303)


@router.post("/admin/users")
def create_user(
    request: Request,
    organization_id: str = Form(...),
    facility_id: str = Form(""),
    full_name: str = Form(...),
    email: str = Form(...),
    job_title: str = Form(""),
    role: str = Form(...),
    temporary_password: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    try:
        governance_service.create_user(
            db,
            Actor.from_principal(principal, request),
            organization_id=organization_id,
            facility_id=facility_id or None,
            full_name=full_name,
            email=email,
            job_title=job_title,
            role=role,
            temporary_password=temporary_password,
        )
    except ServiceError as error:
        flash(request, _service_message(error, current_lang(request)), "critical")
    else:
        flash(request, t("governance.user_created", language=current_lang(request)), "safe")
    return RedirectResponse("/admin#new-user", status_code=303)


@router.post("/admin/users/{user_id}/reset-password")
def reset_user_password(
    request: Request,
    user_id: str,
    temporary_password: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    try:
        governance_service.reset_user_password(
            db,
            Actor.from_principal(principal, request),
            user_id,
            temporary_password=temporary_password,
        )
    except ServiceError as error:
        flash(request, _service_message(error, current_lang(request)), "critical")
    else:
        flash(request, t("governance.password_reset", language=current_lang(request)), "safe")
    return RedirectResponse("/admin#users", status_code=303)


def _run_optimizer() -> None:
    from scripts.run_optimizer import main

    main()


@router.post("/admin/refresh-intelligence")
def refresh_intelligence(
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    if not principal_can(principal, Permission.RUN_OPTIMIZER):
        raise HTTPException(
            status_code=403,
            detail="Decision intelligence refresh is not permitted",
        )

    actor = Actor.from_principal(principal, request)
    with audited(
        db,
        actor,
        "intelligence.refresh.requested",
        "intelligence_refresh",
        "decision-intelligence",
    ) as entry:
        entry.note(mode="manual_retry", inventory_changed=False)

    from services.intelligence_refresh import run_pending

    background.add_task(
        run_pending,
        force=True,
        requested_by=f"{actor.display_name} <{actor.user_id}>",
    )
    flash(
        request,
        t("governance.intelligence_refresh_started", language=current_lang(request)),
        "info",
    )
    return RedirectResponse("/admin#intelligence-refresh", status_code=303)


@router.post("/admin/run-optimizer")
def run_optimizer(
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    if not principal_can(principal, Permission.RUN_OPTIMIZER):
        raise HTTPException(status_code=403, detail="Optimizer execution is not permitted")
    actor = Actor.from_principal(principal, request)
    with audited(db, actor, "optimizer.run.request", "transfer_plan") as entry:
        entry.note(mode="interactive", inventory_changed=False)
    background.add_task(_run_optimizer)
    flash(request, t("governance.optimizer_started", language=current_lang(request)), "info")
    return RedirectResponse("/insights/transfer-plan", status_code=303)
