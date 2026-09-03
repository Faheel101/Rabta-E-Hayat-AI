"""Human-governed execution of optimizer transfer recommendations."""

from __future__ import annotations

import base64
from io import BytesIO

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import CurrentUser, Permission, Role, can, can_open_page
from db.models import EmergencyIncident, Transfer
from i18n.t import t
from services import transfer_service
from services.audit import Actor, ServiceError
from web.deps import Principal, get_db, require_principal
from web.routers.facility import ENABLED_NAV, nav_counts
from web.templating import current_lang, flash, render

router = APIRouter(prefix="/insights/transfer-plan")

STATUS_FILTERS = (
    "RECOMMENDED",
    "APPROVED",
    "DISPATCHED",
    "IN_TRANSIT",
    "RECEIVED",
    "FAILED_COLD_CHAIN",
    "REJECTED",
    "CANCELLED",
    "SUPERSEDED",
)


def _subject(principal: Principal) -> CurrentUser:
    try:
        role = Role(principal.role)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Unknown role") from exc
    return principal.role_subject(role=role)


def _guard(principal: Principal) -> None:
    if not can_open_page(_subject(principal), "transfers"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your role does not permit transfer operations",
        )


def _allowed(principal: Principal, permission: Permission) -> bool:
    return can(_subject(principal), permission)


def _actor(principal: Principal, request: Request) -> Actor:
    return Actor.from_principal(principal, request)


def _scope(principal: Principal) -> list[str]:
    return principal.scope_facility_ids


def _page(request: Request, principal: Principal, db: Session, **kwargs):
    return render(
        request,
        principal=principal,
        db=db,
        nav_counts=nav_counts(db, principal),
        enabled_nav=ENABLED_NAV,
        **kwargs,
    )


def _capabilities(db: Session, principal: Principal, payload: dict) -> dict:
    record = payload["record"]
    source = payload["is_source"]
    destination = payload["is_destination"]
    outbound = _allowed(principal, Permission.APPROVE_TRANSFER_OUT)
    if principal.role == Role.EMERGENCY_CONTROLLER.value:
        outbound = outbound and bool(
            db.scalar(
                select(EmergencyIncident.id).where(
                    EmergencyIncident.transfer_plan_id == record.plan_id,
                    EmergencyIncident.status == "ACTIVE",
                )
            )
        )
    inbound = _allowed(principal, Permission.ACCEPT_TRANSFER_IN)
    return {
        "approve": source and outbound and record.status == "RECOMMENDED",
        "reject": source and outbound and record.status == "RECOMMENDED",
        "modify": source and outbound and record.status == "RECOMMENDED" and record.units > 1,
        "dispatch": source and outbound and record.status == "APPROVED",
        "depart": source and outbound and record.status == "DISPATCHED",
        "cancel": source and outbound and record.status in {"APPROVED", "DISPATCHED"},
        "receive": destination and inbound and record.status == "IN_TRANSIT",
        "print": record.status in {
            "APPROVED", "DISPATCHED", "IN_TRANSIT", "RECEIVED", "FAILED_COLD_CHAIN"
        },
    }


def _detail_page(
    request: Request,
    principal: Principal,
    db: Session,
    transfer_id: str,
    *,
    error: ServiceError | None = None,
    action: str | None = None,
    status_code: int = 200,
):
    try:
        payload = transfer_service.transfer_workspace(db, _scope(principal), transfer_id)
    except ServiceError as exc:
        if exc.code == "TRANSFER_NOT_FOUND":
            raise HTTPException(status_code=404, detail=exc.message) from exc
        raise
    lang = current_lang(request)
    ai_result = None
    ai_id = request.query_params.get("ai_id")
    if ai_id:
        from services.ai_service import load_interaction

        ai_result = load_interaction(db, _actor(principal, request), ai_id)
    return _page(
        request,
        principal,
        db,
        template="insights/transfer_detail.html",
        context={
            **payload,
            "capabilities": _capabilities(db, principal, payload),
            "rejection_reasons": transfer_service.REJECTION_REASONS,
            "form_error": error,
            "error_action": action,
            "ai_result": ai_result,
        },
        page_title=t("tr.detail_title", language=lang),
        breadcrumbs=[
            {"label": t("nav.transfer_plan", language=lang), "url": "/insights/transfer-plan"},
            {"label": payload["record"].tracking_code or payload["record"].id[:8], "url": None},
        ],
        status_code=status_code,
    )


@router.get("")
def transfer_plan(
    request: Request,
    q: str = Query(""),
    status_filter: str = Query("", alias="status"),
    direction: str = Query(""),
    page: int = Query(1),
    view: str = Query("current"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    if status_filter not in STATUS_FILTERS:
        status_filter = ""
    if direction not in {"", "outbound", "inbound"}:
        direction = ""
    if view not in {"current", "history"}:
        view = "current"
    payload = transfer_service.plan_workspace(
        db,
        _scope(principal),
        status_filter=status_filter,
        direction=direction,
        query=q,
        view=view,
        page=page,
        page_size=15,
    )
    lang = current_lang(request)
    return _page(
        request,
        principal,
        db,
        template="insights/transfer_plan.html",
        context={
            **payload,
            "filters": {"q": q, "status": status_filter, "direction": direction, "view": view},
            "status_filters": STATUS_FILTERS,
        },
        page_title=t("tr.title", language=lang),
    )


@router.get("/track/{tracking_code}")
def tracking_page(
    request: Request,
    tracking_code: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    record = db.scalars(
        select(Transfer).where(Transfer.tracking_code == tracking_code)
    ).first()
    if record is None:
        raise HTTPException(status_code=404, detail="Tracking code not found")
    try:
        payload = transfer_service.transfer_workspace(db, _scope(principal), record.id)
    except ServiceError as exc:
        raise HTTPException(status_code=404, detail="Tracking code not found") from exc
    lang = current_lang(request)
    return _page(
        request,
        principal,
        db,
        template="insights/transfer_tracking.html",
        context=payload,
        page_title=t("tr.tracking_title", language=lang),
        breadcrumbs=[
            {"label": t("nav.transfer_plan", language=lang), "url": "/insights/transfer-plan"},
            {"label": tracking_code, "url": None},
        ],
    )


@router.get("/{transfer_id}/dispatch-slip")
def dispatch_slip(
    request: Request,
    transfer_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        payload = transfer_service.transfer_workspace(db, _scope(principal), transfer_id)
    except ServiceError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    record = payload["record"]
    if record.status not in {"APPROVED", "DISPATCHED", "IN_TRANSIT", "RECEIVED", "FAILED_COLD_CHAIN"}:
        raise HTTPException(status_code=409, detail="Approve the transfer before printing its dispatch slip")

    try:
        import barcode
        import qrcode
        from barcode.writer import SVGWriter
        from qrcode.image.svg import SvgPathImage

        barcodes = {}
        for unit in payload["units"]:
            buffer = BytesIO()
            barcode.get("code128", unit.din, writer=SVGWriter()).write(
                buffer,
                options={"write_text": False, "module_height": 8, "quiet_zone": 1},
            )
            barcodes[unit.id] = base64.b64encode(buffer.getvalue()).decode("ascii")
        tracking_url = str(request.url_for("tracking_page", tracking_code=record.tracking_code))
        qr = qrcode.make(tracking_url, image_factory=SvgPathImage, box_size=8, border=1)
        qr_buffer = BytesIO()
        qr.save(qr_buffer)
        qr_svg = base64.b64encode(qr_buffer.getvalue()).decode("ascii")
    except ImportError as exc:  # pragma: no cover - deployment misconfiguration
        raise HTTPException(status_code=500, detail="Dispatch-label dependencies are not installed") from exc

    return render(
        request,
        "insights/transfer_slip.html",
        {
            **payload,
            "barcodes": barcodes,
            "qr_svg": qr_svg,
            "tracking_url": tracking_url,
        },
        principal=principal,
        db=db,
        page_title=t("tr.dispatch_slip", language=current_lang(request)),
    )


@router.get("/{transfer_id}")
def transfer_detail(
    request: Request,
    transfer_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    return _detail_page(request, principal, db, transfer_id)


def _failed(
    request: Request,
    principal: Principal,
    db: Session,
    transfer_id: str,
    action: str,
    error: ServiceError,
):
    if error.code == "TRANSFER_NOT_FOUND":
        raise HTTPException(status_code=404, detail=error.message)
    return _detail_page(
        request,
        principal,
        db,
        transfer_id,
        error=error,
        action=action,
        status_code=422,
    )


@router.post("/{transfer_id}/approve")
def approve(
    request: Request,
    transfer_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        transfer_service.approve_transfer(db, _actor(principal, request), transfer_id)
    except ServiceError as error:
        return _failed(request, principal, db, transfer_id, "approve", error)
    flash(request, t("tr.approved_toast", language=current_lang(request)), "safe")
    return RedirectResponse(f"/insights/transfer-plan/{transfer_id}", status_code=303)


@router.post("/{transfer_id}/reject")
def reject(
    request: Request,
    transfer_id: str,
    reason: str = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        transfer_service.reject_transfer(
            db, _actor(principal, request), transfer_id, reason, note
        )
    except ServiceError as error:
        return _failed(request, principal, db, transfer_id, "reject", error)
    flash(request, t("tr.rejected_toast", language=current_lang(request)), "info")
    return RedirectResponse(f"/insights/transfer-plan/{transfer_id}", status_code=303)


@router.post("/{transfer_id}/modify")
def modify(
    request: Request,
    transfer_id: str,
    units: int = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        transfer_service.modify_transfer_units(
            db, _actor(principal, request), transfer_id, units
        )
    except ServiceError as error:
        return _failed(request, principal, db, transfer_id, "modify", error)
    flash(request, t("tr.modified_toast", language=current_lang(request)), "info")
    return RedirectResponse(f"/insights/transfer-plan/{transfer_id}", status_code=303)


@router.post("/{transfer_id}/dispatch")
def dispatch(
    request: Request,
    transfer_id: str,
    custodian: str = Form(...),
    courier_name: str = Form(...),
    courier_phone: str = Form(""),
    vehicle_ref: str = Form(""),
    container_id: str = Form(...),
    seal_number: str = Form(...),
    departure_temp_c: float = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        transfer_service.dispatch_transfer(
            db,
            _actor(principal, request),
            transfer_id,
            custodian=custodian,
            courier_name=courier_name,
            courier_phone=courier_phone,
            vehicle_ref=vehicle_ref,
            container_id=container_id,
            seal_number=seal_number,
            departure_temp_c=departure_temp_c,
        )
    except ServiceError as error:
        return _failed(request, principal, db, transfer_id, "dispatch", error)
    flash(request, t("tr.dispatched_toast", language=current_lang(request)), "safe")
    return RedirectResponse(f"/insights/transfer-plan/{transfer_id}", status_code=303)


@router.post("/{transfer_id}/depart")
def depart(
    request: Request,
    transfer_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        transfer_service.mark_in_transit(db, _actor(principal, request), transfer_id)
    except ServiceError as error:
        return _failed(request, principal, db, transfer_id, "depart", error)
    flash(request, t("tr.departed_toast", language=current_lang(request)), "safe")
    return RedirectResponse(f"/insights/transfer-plan/{transfer_id}", status_code=303)


@router.post("/{transfer_id}/receive")
def receive(
    request: Request,
    transfer_id: str,
    received_unit_ids: list[str] = Form(default=[]),
    accepted_unit_ids: list[str] = Form(default=[]),
    receiving_temp_c: float = Form(...),
    seal_status: str = Form(...),
    storage_location_id: str = Form(""),
    discrepancy_note: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        transfer_service.receive_transfer(
            db,
            _actor(principal, request),
            transfer_id,
            received_unit_ids=received_unit_ids,
            accepted_unit_ids=accepted_unit_ids,
            receiving_temp_c=receiving_temp_c,
            seal_status=seal_status,
            storage_location_id=storage_location_id or None,
            discrepancy_note=discrepancy_note,
        )
    except ServiceError as error:
        return _failed(request, principal, db, transfer_id, "receive", error)
    flash(request, t("tr.received_toast", language=current_lang(request)), "safe")
    return RedirectResponse(f"/insights/transfer-plan/{transfer_id}", status_code=303)


@router.post("/{transfer_id}/cancel")
def cancel(
    request: Request,
    transfer_id: str,
    reason: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        transfer_service.cancel_transfer(
            db, _actor(principal, request), transfer_id, reason
        )
    except ServiceError as error:
        return _failed(request, principal, db, transfer_id, "cancel", error)
    flash(request, t("tr.cancelled_toast", language=current_lang(request)), "info")
    return RedirectResponse(f"/insights/transfer-plan/{transfer_id}", status_code=303)
