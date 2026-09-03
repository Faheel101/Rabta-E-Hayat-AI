"""Visible, tenant-scoped Qwen assistance and its governance workspace."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Permission, can_open_page
from db.models import AiInteraction
from i18n.t import t
from services import (
    ai_service,
    governance_service,
    insight_service,
    simulation_service,
    transfer_service,
)
from services.audit import Actor, ServiceError
from web.deps import Principal, get_db, principal_can, require_principal
from web.routers.auth import safe_redirect
from web.routers.facility import ENABLED_NAV, nav_counts
from web.templating import current_lang, flash, render


router = APIRouter()


def _actor(principal: Principal, request: Request) -> Actor:
    return Actor.from_principal(principal, request)


def _page(request: Request, principal: Principal, db: Session, **kwargs):
    return render(
        request,
        principal=principal,
        db=db,
        nav_counts=nav_counts(db, principal),
        enabled_nav=ENABLED_NAV,
        **kwargs,
    )


def _can_plan(principal: Principal) -> bool:
    return can_open_page(principal.role_subject(), "command_centre")


def _command_payload(db: Session, principal: Principal, facility_id: str | None = None):
    selected_id = principal.require_scope_facility(facility_id or principal.facility_id)
    selected = next(
        facility for facility in principal.scope_facilities if facility.id == selected_id
    )
    payload = insight_service.command_centre(
        db,
        principal.scope_facility_ids,
        selected_id,
    )
    return payload, selected


def _redirect_with_result(next_path: str | None, interaction_id: str, default: str) -> RedirectResponse:
    target = safe_redirect(next_path, default=default)
    separator = "&" if "?" in target else "?"
    return RedirectResponse(
        f"{target}{separator}ai_id={quote(interaction_id)}",
        status_code=303,
    )


def _workspace_context(
    request: Request,
    db: Session,
    principal: Principal,
    *,
    result=None,
    form_error: ServiceError | None = None,
    question: str = "",
) -> dict:
    counts = nav_counts(db, principal)
    command_payload = None
    guardian = []
    if _can_plan(principal):
        command_payload, _ = _command_payload(db, principal)
        quality = command_payload.get("quality") or {}
        feeds = command_payload.get("feeds") or {}
        guardian = [
            {
                "label": "forecast_quality",
                "value": f"{quality.get('gates_passed', 0)}/{quality.get('gates_total', 4)}",
                "tone": "safe" if quality.get("gates_passed") == quality.get("gates_total") else "warn",
            },
            {
                "label": "feed_health",
                "value": f"{feeds.get('healthy', 0)}/{feeds.get('total', 0)}",
                "tone": "safe" if feeds.get("healthy") == feeds.get("total") else "warn",
            },
            {
                "label": "fallback_series",
                "value": str(quality.get("series_fallback", 0)),
                "tone": "warn" if quality.get("series_fallback") else "safe",
            },
        ]
    recent = list(
        db.scalars(
            select(AiInteraction)
            .where(
                AiInteraction.actor_user_id == principal.user_id,
                AiInteraction.organization_id == principal.organization_id,
            )
            .order_by(AiInteraction.created_at.desc())
            .limit(8)
        ).all()
    )
    return {
        "ai_runtime": ai_service.runtime_status(),
        "ai_result": result,
        "form_error": form_error,
        "question": question,
        "guardian": guardian,
        "recent_ai": recent,
        "can_plan": _can_plan(principal),
        "can_administer_ai": principal.role in {"PROVINCIAL_ADMIN", "SYSTEM_ADMIN"}
        and principal_can(principal, Permission.VIEW_AUDIT_LOG),
        "work_counts": counts,
        "command_payload": command_payload,
    }


@router.get("/ai")
def ai_workspace(
    request: Request,
    ai_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    result = (
        ai_service.load_interaction(db, _actor(principal, request), ai_id)
        if ai_id
        else None
    )
    return _page(
        request,
        principal,
        db,
        template="ai/workspace.html",
        context=_workspace_context(request, db, principal, result=result),
        page_title=t("ai.title", language=current_lang(request)),
    )


@router.post("/ai/ask")
def ask_rabta(
    request: Request,
    question: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    actor = _actor(principal, request)
    counts = nav_counts(db, principal)
    command_payload = None
    if _can_plan(principal):
        command_payload, _ = _command_payload(db, principal)
    facts = ai_service.assistant_facts(
        principal,
        command_payload=command_payload,
        navigation_counts=counts,
    )
    try:
        result = ai_service.generate(
            db,
            actor,
            feature="ask_rabta",
            language=current_lang(request),
            facts=facts,
            question=question,
            fallback=ai_service.assistant_fallback,
        )
    except ServiceError as exc:
        return _page(
            request,
            principal,
            db,
            template="ai/workspace.html",
            context=_workspace_context(
                request,
                db,
                principal,
                form_error=exc,
                question=question,
            ),
            page_title=t("ai.title", language=current_lang(request)),
            status_code=422,
        )
    return _page(
        request,
        principal,
        db,
        template="ai/workspace.html",
        context=_workspace_context(request, db, principal, result=result),
        page_title=t("ai.title", language=current_lang(request)),
    )


@router.post("/ai/command-brief")
def generate_command_brief(
    request: Request,
    facility_id: str = Form(""),
    next: str = Form("/insights/command-centre"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    if not _can_plan(principal):
        raise HTTPException(status_code=403, detail="Planning intelligence is not available to this role")
    payload, selected = _command_payload(db, principal, facility_id or None)
    facts = ai_service.command_facts(
        payload,
        selected_facility=selected,
        scope_facilities=principal.scope_facilities,
    )
    result = ai_service.generate(
        db,
        _actor(principal, request),
        feature="command_brief",
        language=current_lang(request),
        facts=facts,
        fallback=ai_service.command_fallback,
    )
    return _redirect_with_result(next, result.interaction_id, "/insights/command-centre")


@router.post("/ai/forecast-guardian")
def generate_forecast_guardian(
    request: Request,
    facility_id: str = Form(""),
    next: str = Form("/ai"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    if not _can_plan(principal):
        raise HTTPException(status_code=403, detail="Forecast evidence is not available to this role")
    payload, selected = _command_payload(db, principal, facility_id or None)
    facts = ai_service.command_facts(
        payload,
        selected_facility=selected,
        scope_facilities=principal.scope_facilities,
    )
    result = ai_service.generate(
        db,
        _actor(principal, request),
        feature="forecast_guardian",
        language=current_lang(request),
        facts=facts,
        fallback=ai_service.forecast_fallback,
    )
    return _redirect_with_result(next, result.interaction_id, "/ai")


@router.post("/ai/transfer/{transfer_id}")
def generate_transfer_rationale(
    request: Request,
    transfer_id: str,
    next: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    if not can_open_page(principal.role_subject(), "transfers"):
        raise HTTPException(status_code=403, detail="Transfer evidence is not available to this role")
    try:
        payload = transfer_service.transfer_workspace(
            db, principal.scope_facility_ids, transfer_id
        )
    except ServiceError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    result = ai_service.generate(
        db,
        _actor(principal, request),
        feature="transfer_rationale",
        language=current_lang(request),
        facts=ai_service.transfer_facts(payload),
        fallback=ai_service.transfer_fallback,
    )
    default = f"/insights/transfer-plan/{transfer_id}"
    return _redirect_with_result(next or default, result.interaction_id, default)


@router.post("/ai/emergency/{run_id}")
def generate_emergency_brief(
    request: Request,
    run_id: str,
    next: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    if not can_open_page(principal.role_subject(), "simulator"):
        raise HTTPException(status_code=403, detail="Emergency evidence is not available to this role")
    actor = _actor(principal, request)
    try:
        selected = simulation_service.get_simulation_run(db, actor, run_id)
    except ServiceError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    result = ai_service.generate(
        db,
        actor,
        feature="emergency_brief",
        language=current_lang(request),
        facts=ai_service.emergency_facts(selected),
        fallback=ai_service.emergency_fallback,
    )
    default = f"/insights/simulator?run_id={run_id}"
    return _redirect_with_result(next or default, result.interaction_id, default)


def _require_ai_admin(principal: Principal) -> None:
    if principal.role not in {"PROVINCIAL_ADMIN", "SYSTEM_ADMIN"} or not principal_can(
        principal, Permission.VIEW_AUDIT_LOG
    ):
        raise HTTPException(status_code=403, detail="AI governance is not available to this role")


@router.get("/admin/ai")
def ai_administration(
    request: Request,
    ai_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _require_ai_admin(principal)
    actor = _actor(principal, request)
    result = ai_service.load_interaction(db, actor, ai_id) if ai_id else None
    return _page(
        request,
        principal,
        db,
        template="ai/admin.html",
        context={
            **ai_service.administration_snapshot(db, actor),
            "ai_result": result,
        },
        page_title=t("ai.admin_title", language=current_lang(request)),
        breadcrumbs=[
            {"label": t("nav.admin", language=current_lang(request)), "url": "/admin"},
            {"label": t("ai.admin_title", language=current_lang(request)), "url": None},
        ],
    )


@router.post("/admin/ai/test")
def test_qwen_connection(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _require_ai_admin(principal)
    ai_service.reset_circuit_for_tests()
    facts = {
        "provider": "Qwen",
        "model": ai_service.runtime_status()["model"],
        "test": "Return a concise acknowledgement that the governed read-only connection is available.",
        "authority": "No clinical or inventory operation is permitted.",
    }
    result = ai_service.generate(
        db,
        _actor(principal, request),
        feature="ask_rabta",
        language=current_lang(request),
        facts=facts,
        question="Confirm the governed read-only connection using only the supplied facts.",
        fallback=ai_service.assistant_fallback,
        force=True,
    )
    tone = "safe" if result.verified else "warn"
    flash(
        request,
        t(
            "ai.connection_verified" if result.verified else "ai.connection_fallback",
            language=current_lang(request),
        ),
        tone,
    )
    return RedirectResponse(f"/admin/ai?ai_id={result.interaction_id}", status_code=303)


@router.post("/admin/ai/optimizer-advice")
def generate_optimizer_advice(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _require_ai_admin(principal)
    actor = _actor(principal, request)
    result = ai_service.generate(
        db,
        actor,
        feature="optimizer_advisor",
        language=current_lang(request),
        facts=ai_service.optimizer_facts(
            db,
            actor,
            weights=governance_service.optimizer_weights(db),
        ),
        fallback=ai_service.optimizer_fallback,
    )
    return RedirectResponse(f"/admin/ai?ai_id={result.interaction_id}", status_code=303)
