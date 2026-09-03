"""Collection sessions and the chair-side screening wizard.

Routes are thin on purpose. Every write goes through `services/`, which holds the
clinical rules, the permission checks and the audit entry — so the camp tablet,
an import, or a correction screen built later all get the same refusals without
anyone remembering to re-implement them.

The wizard posts each step back to the server, which saves the draft and returns
the next step together with a freshly rendered verdict. The verdict is never
computed in the browser: `core.eligibility` is the only place a deferral decision
is made, and duplicating any part of it into JavaScript is how the screen and the
record start disagreeing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import Permission
from core import eligibility
from core.clock import DEMO_DATETIME
from db.models import (
    BloodGroup,
    Donation,
    DonationSession,
    Donor,
    DonorScreening,
)
from i18n.t import t
from services import screening as screening_service
from services import sessions as session_service
from services.audit import Actor, PermissionDenied, ServiceError
from web.deps import Principal, get_db, require_permission, require_principal
from web.routers.facility import ENABLED_NAV, nav_counts
from web.templating import current_lang, flash, render

router = APIRouter(
    prefix="/app/sessions",
    dependencies=[Depends(require_permission(Permission.COLLECT_DONATION))],
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


def _actor(principal: Principal, request: Request) -> Actor:
    return Actor.from_principal(principal, request)


def _handle(request: Request, error: ServiceError) -> None:
    """Surface a service refusal as a flash message.

    The service raises with a stable code; the route decides how to show it. No
    route re-implements the rule that produced it.
    """

    flash(request, error.message, "error")


# ------------------------------------------------------------------- sessions


@router.get("")
def session_list(
    request: Request,
    status: str = Query(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    lang = current_lang(request)
    facility_ids = principal.facility_ids(
        "organization" if principal.is_group_user else "facility"
    )

    statement = select(DonationSession).where(
        DonationSession.facility_id.in_(facility_ids)
    )

    if status:
        statement = statement.where(DonationSession.status == status)

    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0

    rows = db.scalars(
        statement.order_by(
            DonationSession.scheduled_date.desc(), DonationSession.session_code
        ).limit(60)
    ).all()

    # Counted once for the whole page rather than per row.
    ids = [row.id for row in rows]
    collected = dict(
        db.execute(
            select(Donation.session_id, func.count())
            .where(Donation.session_id.in_(ids))
            .group_by(Donation.session_id)
        ).all()
    )
    screened = dict(
        db.execute(
            select(DonorScreening.session_id, func.count())
            .where(
                DonorScreening.session_id.in_(ids),
                DonorScreening.outcome.in_(screening_service.FINAL_OUTCOMES),
            )
            .group_by(DonorScreening.session_id)
        ).all()
    )

    open_now = session_service.current_session(db, _actor(principal, request))

    return _page(
        request,
        principal,
        db,
        template="app/sessions.html",
        context={
            "rows": [
                {
                    "session": row,
                    "screened": screened.get(row.id, 0),
                    "collected": collected.get(row.id, 0),
                }
                for row in rows
            ],
            "total": total,
            "shown": len(rows),
            "open_session": open_now,
            "session_types": session_service.SESSION_TYPES,
            "filters": {"status": status},
            "can_open": principal.role
            in ("BLOOD_BANK_OFFICER", "RBC_COORDINATOR", "PROVINCIAL_ADMIN", "SYSTEM_ADMIN", "PHLEBOTOMIST"),
        },
        page_title=t("nav.sessions", language=lang),
        breadcrumbs=[
            {"label": t("nav.dashboard", language=lang), "url": "/app/dashboard"},
            {"label": t("nav.sessions", language=lang), "url": "/app/sessions"},
        ],
    )


@router.post("/open")
def open_session(
    request: Request,
    session_type: str = Form("IN_HOUSE"),
    venue: str = Form(""),
    organiser: str = Form(""),
    contact_phone: str = Form(""),
    target_units: int = Form(0),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    try:
        record = session_service.open_session(
            db,
            _actor(principal, request),
            session_type=session_type,
            venue=venue or None,
            organiser=organiser or None,
            contact_phone=contact_phone or None,
            target_units=target_units,
        )
    except (ServiceError, PermissionDenied) as error:
        _handle(request, error)
        return RedirectResponse("/app/sessions", status_code=303)

    flash(
        request,
        t(
            "ops.session_opened_flash",
            language=current_lang(request),
            code=record.session_code,
        ),
        "success",
    )

    return RedirectResponse(f"/app/sessions/{record.id}", status_code=303)


@router.post("/{session_id}/close")
def close_session(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    try:
        session_service.close_session(
            db, _actor(principal, request), session_id=session_id
        )
        flash(
            request,
            t("ops.session_closed_flash", language=current_lang(request)),
            "success",
        )
    except (ServiceError, PermissionDenied) as error:
        _handle(request, error)

    return RedirectResponse(f"/app/sessions/{session_id}", status_code=303)


@router.get("/{session_id}")
def session_detail(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    lang = current_lang(request)
    facility_ids = principal.facility_ids(
        "organization" if principal.is_group_user else "facility"
    )

    record = db.scalars(
        select(DonationSession).where(
            DonationSession.id == session_id,
            DonationSession.facility_id.in_(facility_ids),
        )
    ).first()

    if record is None:
        raise HTTPException(status_code=404, detail="Session not found")

    summary = session_service.session_summary(db, session_id)

    # Deferral reasons in aggregate, never against a name. A session page is the
    # most casually shared screen in this system, and a list of who was deferred
    # for what is a list of people's health findings.
    reasons = db.execute(
        select(DonorScreening.deferral_reason_code, func.count())
        .where(
            DonorScreening.session_id == session_id,
            DonorScreening.outcome == "DEFERRED",
        )
        .group_by(DonorScreening.deferral_reason_code)
        .order_by(func.count().desc())
    ).all()

    # The one place a name appears: an unfinished screening somebody has to pick
    # up. That is the whole reason drafts are stored.
    drafts = screening_service.open_drafts(db, session_id=session_id)
    draft_donors = {}

    if drafts:
        draft_donors = {
            row.id: row
            for row in db.scalars(
                select(Donor).where(Donor.id.in_([d.donor_id for d in drafts]))
            ).all()
        }

    return _page(
        request,
        principal,
        db,
        template="app/session_detail.html",
        context={
            "session": record,
            "summary": summary,
            "reasons": reasons,
            "drafts": [
                {"screening": draft, "donor": draft_donors.get(draft.donor_id)}
                for draft in drafts
            ],
            "is_camp": record.session_type != "IN_HOUSE",
            "can_screen": principal.role
            in (
                "PHLEBOTOMIST",
                "BLOOD_BANK_OFFICER",
                "RBC_COORDINATOR",
                "PROVINCIAL_ADMIN",
                "SYSTEM_ADMIN",
            ),
        },
        page_title=record.session_code,
        breadcrumbs=[
            {"label": t("nav.dashboard", language=lang), "url": "/app/dashboard"},
            {"label": t("nav.sessions", language=lang), "url": "/app/sessions"},
            {"label": record.session_code, "url": f"/app/sessions/{record.id}"},
        ],
    )


# --------------------------------------------------------------- the wizard


def _own_session(db: Session, principal: Principal, session_id: str) -> DonationSession:
    facility_ids = principal.facility_ids(
        "organization" if principal.is_group_user else "facility"
    )

    record = db.scalars(
        select(DonationSession).where(
            DonationSession.id == session_id,
            DonationSession.facility_id.in_(facility_ids),
        )
    ).first()

    if record is None:
        raise HTTPException(status_code=404, detail="Session not found")

    return record


@router.get("/{session_id}/screen")
def wizard_start(
    session_id: str,
    request: Request,
    q: str = Query(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    """Step one: find the donor, or register them.

    At a camp most donors are not on the register yet, and turning somebody away
    to fill in a form elsewhere loses the donation.
    """

    lang = current_lang(request)
    record = _own_session(db, principal, session_id)

    matches = []

    if q and len(q.strip()) >= 2:
        needle = f"%{q.strip()}%"
        matches = db.scalars(
            select(Donor)
            .where(
                Donor.registered_facility_id == record.facility_id,
                or_(
                    Donor.full_name.ilike(needle),
                    Donor.donor_code.ilike(needle),
                    Donor.phone.ilike(needle),
                    Donor.cnic_last4 == q.strip()[-4:],
                ),
            )
            .order_by(Donor.full_name)
            .limit(12)
        ).all()

    groups = db.execute(select(BloodGroup.id, BloodGroup.code).order_by(BloodGroup.id)).all()

    return _page(
        request,
        principal,
        db,
        template="app/screen_find.html",
        context={
            "session": record,
            "query": q,
            "matches": matches,
            "searched": bool(q and len(q.strip()) >= 2),
            "groups": groups,
            "is_camp": record.session_type != "IN_HOUSE",
        },
        page_title=t("ops.screen_donor", language=lang),
        breadcrumbs=[
            {"label": t("nav.sessions", language=lang), "url": "/app/sessions"},
            {"label": record.session_code, "url": f"/app/sessions/{record.id}"},
            {
                "label": t("ops.screening", language=lang),
                "url": f"/app/sessions/{record.id}/screen",
            },
        ],
    )


@router.post("/{session_id}/screen/start")
def wizard_open_draft(
    session_id: str,
    request: Request,
    donor_id: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _own_session(db, principal, session_id)

    try:
        draft = screening_service.start_screening(
            db, _actor(principal, request), donor_id=donor_id, session_id=session_id
        )
    except (ServiceError, PermissionDenied) as error:
        _handle(request, error)
        return RedirectResponse(f"/app/sessions/{session_id}/screen", status_code=303)

    return RedirectResponse(
        f"/app/sessions/{session_id}/screen/{draft.id}", status_code=303
    )


def _wizard_context(db: Session, record: DonorScreening, session_row) -> dict:
    donor = db.get(Donor, record.donor_id)
    verdict = screening_service.current_verdict(db, record, donor)
    group = db.scalar(
        select(BloodGroup.code).where(BloodGroup.id == donor.blood_group_id)
    )

    donation = db.scalars(
        select(Donation).where(Donation.screening_id == record.id)
    ).first()

    return {
        "session": session_row,
        "screening": record,
        "donor": donor,
        "group": group,
        "verdict": verdict,
        "questions": eligibility.questions_for(
            "FEMALE" if (donor.gender or "").upper() == "FEMALE" else "MALE"
        ),
        "answers": record.questionnaire_json or {},
        "donation": donation,
        "is_camp": session_row.session_type != "IN_HOUSE",
        "is_draft": record.outcome == screening_service.DRAFT,
    }


@router.get("/{session_id}/screen/{screening_id}")
def wizard(
    session_id: str,
    screening_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    lang = current_lang(request)
    session_row = _own_session(db, principal, session_id)

    record = db.scalars(
        select(DonorScreening).where(
            DonorScreening.id == screening_id,
            DonorScreening.session_id == session_id,
        )
    ).first()

    if record is None:
        raise HTTPException(status_code=404, detail="Screening not found")

    context = _wizard_context(db, record, session_row)

    return _page(
        request,
        principal,
        db,
        template="app/screen_wizard.html",
        context=context,
        page_title=f"{t('ops.screening', language=lang)} · {context['donor'].full_name or context['donor'].donor_code}",
        breadcrumbs=[
            {"label": t("nav.sessions", language=lang), "url": "/app/sessions"},
            {"label": session_row.session_code, "url": f"/app/sessions/{session_id}"},
            {"label": t("ops.screening", language=lang), "url": request.url.path},
        ],
    )


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@router.post("/{session_id}/screen/{screening_id}/vitals", response_class=HTMLResponse)
def wizard_vitals(
    session_id: str,
    screening_id: str,
    request: Request,
    haemoglobin_g_dl: str = Form(""),
    weight_kg: str = Form(""),
    systolic_bp: str = Form(""),
    diastolic_bp: str = Form(""),
    pulse_bpm: str = Form(""),
    temperature_c: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    """Save the vitals step and return the verdict as it now stands."""

    session_row = _own_session(db, principal, session_id)

    try:
        record, _ = screening_service.save_draft(
            db,
            _actor(principal, request),
            screening_id=screening_id,
            vitals={
                "haemoglobin_g_dl": _float(haemoglobin_g_dl),
                "weight_kg": _float(weight_kg),
                "systolic_bp": _int(systolic_bp),
                "diastolic_bp": _int(diastolic_bp),
                "pulse_bpm": _int(pulse_bpm),
                "temperature_c": _float(temperature_c),
            },
        )
    except (ServiceError, PermissionDenied) as error:
        _handle(request, error)
        return RedirectResponse(
            f"/app/sessions/{session_id}/screen/{screening_id}", status_code=303
        )

    return _fragment(request, db, record, session_row)


@router.post(
    "/{session_id}/screen/{screening_id}/questions", response_class=HTMLResponse
)
async def wizard_questions(
    session_id: str,
    screening_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    """Save the questionnaire.

    Read from the raw form rather than declared parameters because the question
    set is sex-dependent and comes from `core.eligibility`, not from a signature
    here — restating the keys would be a second place for them to drift.
    """

    session_row = _own_session(db, principal, session_id)
    form = await request.form()

    answers = {
        question["key"]: form.get(question["key"]) in ("1", "true", "on", "yes")
        for question in eligibility.QUESTIONS
    }

    try:
        record, _ = screening_service.save_draft(
            db, _actor(principal, request), screening_id=screening_id, answers=answers
        )
    except (ServiceError, PermissionDenied) as error:
        _handle(request, error)
        return RedirectResponse(
            f"/app/sessions/{session_id}/screen/{screening_id}", status_code=303
        )

    return _fragment(request, db, record, session_row)


def _fragment(request: Request, db: Session, record: DonorScreening, session_row):
    """The wizard body, re-rendered server-side after a step is saved.

    Only the body is swapped, not the page, so the donor header and breadcrumbs
    stay put while the verdict updates.
    """

    from web.templating import templates

    return templates.TemplateResponse(
        request, "app/_screen_body.html", _wizard_context(db, record, session_row)
    )


@router.post("/{session_id}/screen/{screening_id}/complete")
def wizard_complete(
    session_id: str,
    screening_id: str,
    request: Request,
    notes: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _own_session(db, principal, session_id)

    try:
        record, verdict = screening_service.finalise_screening(
            db,
            _actor(principal, request),
            screening_id=screening_id,
            notes=notes or None,
        )
    except (ServiceError, PermissionDenied) as error:
        _handle(request, error)
        return RedirectResponse(
            f"/app/sessions/{session_id}/screen/{screening_id}", status_code=303
        )

    if verdict.accepted:
        flash(
            request,
            t("ops.screening_accepted_flash", language=current_lang(request)),
            "success",
        )
    else:
        flash(
            request,
            t("ops.screening_deferred_flash", language=current_lang(request)),
            "info",
        )

    return RedirectResponse(
        f"/app/sessions/{session_id}/screen/{screening_id}", status_code=303
    )


@router.post("/{session_id}/screen/{screening_id}/collect")
def wizard_collect(
    session_id: str,
    screening_id: str,
    request: Request,
    bag_type: str = Form("TRIPLE"),
    donation_type: str = Form("WHOLE_BLOOD"),
    adverse_reaction: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _own_session(db, principal, session_id)

    try:
        donation = screening_service.record_donation(
            db,
            _actor(principal, request),
            screening_id=screening_id,
            donation_type=donation_type,
            bag_type=bag_type,
            adverse_reaction=adverse_reaction or None,
        )
    except (ServiceError, PermissionDenied) as error:
        _handle(request, error)
        return RedirectResponse(
            f"/app/sessions/{session_id}/screen/{screening_id}", status_code=303
        )

    flash(
        request,
        t(
            "ops.collection_recorded_flash",
            language=current_lang(request),
            din=donation.din,
        ),
        "success",
    )

    return RedirectResponse(f"/app/sessions/{session_id}/screen", status_code=303)


@router.post("/{session_id}/screen/{screening_id}/abandon")
def wizard_abandon(
    session_id: str,
    screening_id: str,
    request: Request,
    reason: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _own_session(db, principal, session_id)

    try:
        screening_service.abandon_draft(
            db,
            _actor(principal, request),
            screening_id=screening_id,
            reason=reason or "Abandoned at the chair.",
        )
        flash(
            request,
            t("ops.screening_abandoned_flash", language=current_lang(request)),
            "info",
        )
    except (ServiceError, PermissionDenied) as error:
        _handle(request, error)

    return RedirectResponse(f"/app/sessions/{session_id}", status_code=303)
