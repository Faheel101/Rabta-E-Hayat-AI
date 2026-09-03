"""Predictive intelligence: command centre, forecast and expiry rescue."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.auth import Permission, Role, can_open_page
from db.models import BloodGroup, BloodUnit, Component, Facility, StorageLocation
from i18n.t import t
from services import insight_service
from web.deps import Principal, get_db, principal_can, require_principal
from web.routers.facility import ENABLED_NAV, nav_counts
from web.templating import current_lang, render

router = APIRouter(prefix="/insights")


def _guard(principal: Principal, page_key: str) -> None:
    try:
        role = Role(principal.role)
    except ValueError:
        raise HTTPException(status_code=403, detail="Unknown role")

    subject = principal.role_subject(role=role)
    if not can_open_page(subject, page_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your role does not permit this intelligence view",
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


def _selected_facility(principal: Principal, facility_id: str | None) -> str:
    selected = facility_id or principal.facility_id
    return principal.require_scope_facility(selected)


def _optional_filter_id(value: str | None) -> int | None:
    """Parse an optional select value without rejecting the HTML blank option."""

    if not value:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


@router.get("/command-centre")
def command_centre(
    request: Request,
    facility_id: str | None = Query(default=None),
    ai_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal, "command_centre")
    selected = _selected_facility(principal, facility_id)
    scope_ids = principal.scope_facility_ids
    payload = insight_service.command_centre(db, scope_ids, selected)
    lang = current_lang(request)
    ai_result = None
    if ai_id:
        from services.ai_service import load_interaction
        from services.audit import Actor

        ai_result = load_interaction(
            db,
            Actor.from_principal(principal, request),
            ai_id,
        )

    return _page(
        request,
        principal,
        db,
        template="insights/command_centre.html",
        context={
            **payload,
            "selected_facility_id": selected,
            "scope_facilities": principal.scope_facilities,
            "scope_is_organization": len(principal.scope_facilities) > 1,
            "ai_result": ai_result,
        },
        page_title=t("nav.command_centre", language=lang),
    )


@router.get("/forecast")
def forecast(
    request: Request,
    facility_id: str | None = Query(default=None),
    component_id: str | None = Query(default=None),
    blood_group_id: str | None = Query(default=None),
    horizon: int = Query(default=14),
    view: str = Query(default="chart"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal, "forecast")
    selected_facility = _selected_facility(principal, facility_id)
    selected_component_id = _optional_filter_id(component_id)
    selected_blood_group_id = _optional_filter_id(blood_group_id)
    horizon = horizon if horizon in {7, 14, 30} else 14
    view = view if view in {"chart", "table", "compare"} else "chart"
    payload = insight_service.forecast_detail(
        db,
        selected_facility,
        selected_component_id,
        selected_blood_group_id,
        horizon,
    )
    lang = current_lang(request)
    facility = next(
        item for item in principal.scope_facilities if item.id == selected_facility
    )

    return _page(
        request,
        principal,
        db,
        template="insights/forecast.html",
        context={
            **payload,
            "selected_facility_id": selected_facility,
            "selected_facility": facility,
            "scope_facilities": principal.scope_facilities,
            "horizon": horizon,
            "view": view,
        },
        page_title=t("fc.title", language=lang),
    )


@router.get("/expiry-rescue")
def expiry_rescue(
    request: Request,
    facility_id: str | None = Query(default=None),
    component_id: str | None = Query(default=None),
    tier: str | None = Query(default="ACTIONABLE"),
    sort: str = Query(default="deadline"),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal, "expiry")
    scope_ids = principal.scope_facility_ids
    selected_component_id = _optional_filter_id(component_id)
    if facility_id:
        principal.require_scope_facility(facility_id)
    if tier == "":
        tier = None
    if tier not in {
        None,
        "ACTIONABLE",
        "ACT_NOW",
        "WATCH",
        "UNRESCUABLE",
        "NOT_TRANSFERABLE",
        "SAFE",
    }:
        tier = "ACTIONABLE"
    if sort not in {"deadline", "probability", "value"}:
        sort = "deadline"

    payload = insight_service.expiry_rescue(
        db,
        scope_ids,
        facility_id=facility_id,
        component_id=selected_component_id,
        tier=tier,
        sort_by=sort,
        page=page,
        page_size=10,
    )
    lang = current_lang(request)
    return _page(
        request,
        principal,
        db,
        template="insights/expiry_rescue.html",
        context={
            **payload,
            "scope_facilities": principal.scope_facilities,
            "selected_facility_id": facility_id,
            "selected_component_id": selected_component_id,
            "selected_tier": tier,
            "sort_by": sort,
        },
        page_title=t("ex.title", language=lang),
    )


@router.get("/unit-evidence/{unit_id}")
def unit_evidence(
    request: Request,
    unit_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    """Redacted planning evidence for roles without bench-level unit access."""

    if not principal_can(principal, Permission.VIEW_NETWORK):
        raise HTTPException(status_code=403, detail="Network evidence is not permitted")
    unit = db.get(BloodUnit, unit_id)
    if unit is None or unit.facility_id not in set(principal.scope_facility_ids):
        raise HTTPException(status_code=404, detail="Unit not found in this scope")

    facility = db.get(Facility, unit.facility_id)
    component = db.get(Component, unit.component_id)
    group = db.get(BloodGroup, unit.blood_group_id)
    storage = (
        db.get(StorageLocation, unit.storage_location_id)
        if unit.storage_location_id
        else None
    )
    can_open_operational = (
        principal_can(principal, Permission.VIEW_LOCAL_INVENTORY)
        and principal.owns_facility(unit.facility_id)
    )
    lang = current_lang(request)
    return _page(
        request,
        principal,
        db,
        template="insights/unit_evidence.html",
        context={
            "unit": unit,
            "facility": facility,
            "component": component,
            "group": group,
            "storage": storage,
            "can_open_operational": can_open_operational,
        },
        page_title=t("evidence.title", language=lang),
        breadcrumbs=[
            {
                "label": t("nav.command_centre", language=lang),
                "url": "/insights/command-centre",
            },
            {"label": unit.din, "url": None},
        ],
    )
