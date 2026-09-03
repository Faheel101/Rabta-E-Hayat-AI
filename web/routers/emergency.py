"""Preparedness simulation and explicit live-emergency declaration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import CurrentUser, Permission, Role, can, can_open_page
from db.models import EmergencyIncident
from i18n.t import t
from services import emergency_service, simulation_service
from services.audit import Actor, ServiceError
from web.deps import Principal, get_db, require_principal
from web.routers.facility import ENABLED_NAV, nav_counts
from web.templating import current_lang, flash, render

router = APIRouter(prefix="/insights/simulator")


def _subject(principal: Principal) -> CurrentUser:
    try:
        role = Role(principal.role)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Unknown role") from exc
    return principal.role_subject(role=role)


def _guard(principal: Principal) -> None:
    if not can_open_page(_subject(principal), "simulator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your role does not permit the emergency simulator",
        )


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


def _workspace(
    request: Request,
    principal: Principal,
    db: Session,
    *,
    run_id: str | None = None,
    form_error: ServiceError | None = None,
    form_values: dict | None = None,
    status_code: int = 200,
):
    actor = _actor(principal, request)
    runs = simulation_service.list_simulation_runs(db, actor, limit=20)
    selected = None
    if run_id:
        try:
            selected = simulation_service.get_simulation_run(db, actor, run_id)
        except ServiceError as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc
    elif runs:
        selected = runs[0]
    result = dict(selected.results_json or {}) if selected else None
    declared = None
    if selected:
        declared = db.scalar(
            select(EmergencyIncident).where(
                EmergencyIncident.simulation_run_id == selected.id
            )
        )
    subject = _subject(principal)
    lang = current_lang(request)
    defaults = {
        "name": "Major bus accident — Motorway M2",
        "event_type": "BUS_ACCIDENT",
        "epicenter_lat": 31.6000,
        "epicenter_lon": 74.3000,
        "casualties": 180,
        "severity_minor": 30,
        "severity_moderate": 30,
        "severity_severe": 28,
        "severity_critical": 12,
        "onset_profile": "RAMP_6H",
        "duration_hours": 12,
        "iterations": 1000,
        "seed": 42,
        "impact_radius_km": 80,
        "facilities_degraded_pct": 0,
        "degraded_capacity_loss_pct": 0,
        "roads_blocked": False,
        "release_emergency_reserves": True,
        "emergency_reserve_release_pct": 50,
    }
    defaults.update(form_values or {})
    ai_result = None
    ai_id = request.query_params.get("ai_id")
    if ai_id:
        from services.ai_service import load_interaction

        ai_result = load_interaction(db, actor, ai_id)
    return _page(
        request,
        principal,
        db,
        template="insights/simulator.html",
        context={
            "runs": runs,
            "selected_run": selected,
            "result": result,
            "declared_incident": declared,
            "active_incidents": emergency_service.active_incidents(db, actor),
            "presets": simulation_service.scenario_presets(),
            "form_values": defaults,
            "form_error": form_error,
            "can_run": can(subject, Permission.RUN_SIMULATION),
            "can_declare": can(subject, Permission.DECLARE_EMERGENCY),
            "declaration_phrase": emergency_service.DECLARATION_PHRASE,
            "simulation_mode": True,
            "ai_result": ai_result,
        },
        page_title=t("sim.title", language=lang),
        status_code=status_code,
    )


@router.get("")
def simulator(
    request: Request,
    run_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    return _workspace(request, principal, db, run_id=run_id)


@router.post("/run")
def run_scenario(
    request: Request,
    name: str = Form(...),
    event_type: str = Form(...),
    epicenter_lat: float = Form(...),
    epicenter_lon: float = Form(...),
    casualties: int = Form(...),
    severity_minor: float = Form(...),
    severity_moderate: float = Form(...),
    severity_severe: float = Form(...),
    severity_critical: float = Form(...),
    onset_profile: str = Form(...),
    duration_hours: int = Form(...),
    iterations: int = Form(...),
    seed: int = Form(...),
    impact_radius_km: float = Form(...),
    facilities_degraded_pct: float = Form(0),
    degraded_capacity_loss_pct: float = Form(0),
    roads_blocked: bool = Form(False),
    release_emergency_reserves: bool = Form(False),
    emergency_reserve_release_pct: float = Form(0),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    values = {
        "name": name,
        "event_type": event_type,
        "epicenter_lat": epicenter_lat,
        "epicenter_lon": epicenter_lon,
        "casualties": casualties,
        "severity_minor": severity_minor,
        "severity_moderate": severity_moderate,
        "severity_severe": severity_severe,
        "severity_critical": severity_critical,
        "severity_mix": {
            "MINOR": severity_minor,
            "MODERATE": severity_moderate,
            "SEVERE": severity_severe,
            "CRITICAL": severity_critical,
        },
        "onset_profile": onset_profile,
        "duration_hours": duration_hours,
        "iterations": iterations,
        "seed": seed,
        "impact_radius_km": impact_radius_km,
        "facilities_degraded_pct": facilities_degraded_pct,
        "degraded_capacity_loss_pct": degraded_capacity_loss_pct,
        "roads_blocked": roads_blocked,
        "release_emergency_reserves": release_emergency_reserves,
        "emergency_reserve_release_pct": emergency_reserve_release_pct,
    }
    try:
        result = simulation_service.run_simulation(
            db,
            values,
            save=True,
            actor=_actor(principal, request),
        )
    except ServiceError as exc:
        return _workspace(
            request,
            principal,
            db,
            form_error=exc,
            form_values=values,
            status_code=422,
        )
    flash(request, t("sim.run_complete", language=current_lang(request)), "safe")
    return RedirectResponse(
        f"/insights/simulator?run_id={result['run_id']}", status_code=303
    )


@router.post("/{run_id}/compare")
def compare_scenario(
    request: Request,
    run_id: str,
    roads_blocked: bool = Form(False),
    facilities_degraded_pct: float = Form(0),
    degraded_capacity_loss_pct: float = Form(0),
    release_emergency_reserves: bool = Form(False),
    emergency_reserve_release_pct: float = Form(0),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        result = simulation_service.compare_simulation(
            db,
            _actor(principal, request),
            run_id,
            {
                "name": "Intervention comparison",
                "roads_blocked": roads_blocked,
                "facilities_degraded_pct": facilities_degraded_pct,
                "degraded_capacity_loss_pct": degraded_capacity_loss_pct,
                "release_emergency_reserves": release_emergency_reserves,
                "emergency_reserve_release_pct": emergency_reserve_release_pct,
            },
        )
    except ServiceError as exc:
        flash(request, exc.message, "critical")
        return RedirectResponse(f"/insights/simulator?run_id={run_id}", status_code=303)
    flash(request, t("sim.comparison_complete", language=current_lang(request)), "safe")
    return RedirectResponse(
        f"/insights/simulator?run_id={result['run_id']}", status_code=303
    )


@router.post("/{run_id}/declare")
def declare_live(
    request: Request,
    run_id: str,
    acknowledgement: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        incident = emergency_service.declare_incident(
            db,
            _actor(principal, request),
            run_id,
            acknowledgement,
        )
    except ServiceError as exc:
        flash(request, exc.message, "critical")
        return RedirectResponse(f"/insights/simulator?run_id={run_id}", status_code=303)
    flash(request, t("sim.live_declared", language=current_lang(request)), "critical")
    return RedirectResponse(
        f"/insights/simulator?run_id={incident.simulation_run_id}", status_code=303
    )


@router.post("/incident/{incident_id}/resolve")
def resolve_live(
    request: Request,
    incident_id: str,
    note: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        incident = emergency_service.resolve_incident(
            db,
            _actor(principal, request),
            incident_id,
            note,
        )
    except ServiceError as exc:
        flash(request, exc.message, "critical")
        return RedirectResponse("/insights/simulator", status_code=303)
    flash(request, t("sim.live_resolved", language=current_lang(request)), "safe")
    return RedirectResponse(
        f"/insights/simulator?run_id={incident.simulation_run_id}", status_code=303
    )
