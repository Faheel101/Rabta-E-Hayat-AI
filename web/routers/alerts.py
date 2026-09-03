"""Accountable operational alert queue."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import CurrentUser, Role, can_open_page
from i18n.t import t
from services import alert_service
from services.audit import Actor, ServiceError
from web.deps import Principal, get_db, require_principal
from web.routers.facility import ENABLED_NAV, nav_counts
from web.templating import current_lang, flash, render

router = APIRouter(prefix="/insights/alerts")


def _guard(principal: Principal) -> None:
    try:
        subject = principal.role_subject(role=Role(principal.role))
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Unknown role") from exc
    if not can_open_page(subject, "alerts"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your role does not permit the alert workspace",
        )


def _actor(principal: Principal, request: Request) -> Actor:
    return Actor.from_principal(principal, request)


def _return_url(status_filter: str = "", severity: str = "", alert_type: str = ""):
    parts = []
    if status_filter:
        parts.append("status=" + status_filter)
    if severity:
        parts.append("severity=" + severity)
    if alert_type:
        parts.append("type=" + alert_type)
    return "/insights/alerts" + ("?" + "&".join(parts) if parts else "")


@router.get("")
def alerts(
    request: Request,
    status_filter: str = Query("", alias="status"),
    severity: str = Query(""),
    alert_type: str = Query("", alias="type"),
    page: int = Query(1),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    payload = alert_service.alert_workspace(
        db,
        organization_id=principal.organization_id,
        facility_ids=principal.org_facility_ids,
        status_filter=status_filter,
        severity_filter=severity,
        type_filter=alert_type,
        page=page,
    )
    lang = current_lang(request)
    return render(
        request,
        "insights/alerts.html",
        {
            **payload,
            "filters": {
                "status": status_filter,
                "severity": severity,
                "type": alert_type,
                "page": page,
            },
            "alert_types": sorted(
                alert_service.MANAGED_TYPES | {"SURGE_DETECTED"}
            ),
        },
        principal=principal,
        db=db,
        nav_counts=nav_counts(db, principal),
        enabled_nav=ENABLED_NAV,
        page_title=t("alerts.title", language=lang),
    )


@router.post("/refresh")
def refresh_alerts(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    result = alert_service.sync_operational_alerts(
        db,
        _actor(principal, request),
        principal.org_facility_ids,
    )
    flash(
        request,
        t(
            "alerts.refreshed",
            language=current_lang(request),
            count=result["active_evidence"],
        ),
        "safe",
    )
    return RedirectResponse("/insights/alerts", status_code=303)


@router.post("/{alert_id}/acknowledge")
def acknowledge(
    request: Request,
    alert_id: str,
    note: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        alert_service.acknowledge_alert(
            db,
            _actor(principal, request),
            alert_id,
            note,
        )
    except ServiceError as exc:
        flash(request, exc.message, "critical")
    else:
        flash(request, t("alerts.acknowledged", language=current_lang(request)), "safe")
    return RedirectResponse("/insights/alerts", status_code=303)


@router.post("/{alert_id}/resolve")
def resolve(
    request: Request,
    alert_id: str,
    reason: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        alert_service.resolve_alert(
            db,
            _actor(principal, request),
            alert_id,
            reason,
        )
    except ServiceError as exc:
        flash(request, exc.message, "critical")
    else:
        flash(request, t("alerts.resolved", language=current_lang(request)), "safe")
    return RedirectResponse("/insights/alerts", status_code=303)
