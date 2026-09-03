"""The lab bench: worklist, test runs, and release.

Release is the control point where a quarantined bag becomes issuable stock, so
the page exists mainly to make the two-person rule visible. A donation the
signed-in technologist tested themselves is still shown — with the reason it
cannot be signed here — rather than hidden. A control you cannot see is one
people work around; a control you can see is one people staff for.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Permission
from core.clock import DEMO_DATETIME
from db.models import Donation, DonationTest, Donor, LabRun
from i18n.t import t
from services import lab as service
from services.audit import Actor, PermissionDenied, ServiceError
from web.deps import Principal, get_db, require_permission, require_principal
from web.routers.facility import ENABLED_NAV, nav_counts
from web.templating import current_lang, flash, render

router = APIRouter(
    prefix="/app/lab",
    dependencies=[Depends(require_permission(Permission.PERFORM_TEST))],
)

METHODS = {
    "HIV": "ELISA (4th generation)",
    "HBSAG": "ELISA",
    "HCV": "ELISA (3rd generation)",
    "SYPHILIS": "RPR",
    "MALARIA": "ICT (antigen)",
}


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


@router.get("")
def bench(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    actor = _actor(principal, request)

    pending = service.pending(db, actor)
    ready = service.awaiting_release(db, actor)

    open_runs = db.scalars(
        select(LabRun)
        .where(
            LabRun.facility_id == principal.facility_id,
            LabRun.status.in_(("OPEN", "RESULTS_ENTERED")),
        )
        .order_by(LabRun.opened_at.desc())
        .limit(20)
    ).all()

    # A donation this user tested cannot be released by them. Counted so the
    # page can say so plainly rather than just showing a shorter list.
    blocked = sum(1 for row in ready if not row["releasable_by_actor"])

    lang = current_lang(request)

    return _page(
        request,
        principal,
        db,
        template="app/lab.html",
        context={
            "pending": pending,
            "ready": ready,
            "blocked_by_two_person_rule": blocked,
            "open_runs": open_runs,
            "panel": service.required_tests(),
            "methods": METHODS,
            "can_test": principal.role
            in (
                "LAB_TECHNOLOGIST",
                "BLOOD_BANK_OFFICER",
                "RBC_COORDINATOR",
                "PROVINCIAL_ADMIN",
                "SYSTEM_ADMIN",
            ),
        },
        page_title=t("nav.lab", language=lang),
        breadcrumbs=[
            {"label": t("nav.dashboard", language=lang), "url": "/app/dashboard"},
            {"label": t("nav.lab", language=lang), "url": "/app/lab"},
        ],
    )


@router.post("/runs/open")
def open_run(
    request: Request,
    test_code: str = Form(...),
    kit_lot: str = Form(""),
    kit_expiry: str = Form(""),
    equipment: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    expiry = None

    if kit_expiry:
        try:
            expiry = date.fromisoformat(kit_expiry)
        except ValueError:
            flash(
                request,
                t("ops.invalid_kit_expiry", language=current_lang(request)),
                "error",
            )
            return RedirectResponse("/app/lab", status_code=303)

    try:
        run = service.open_run(
            db,
            _actor(principal, request),
            test_code=test_code,
            method=METHODS.get(test_code),
            kit_lot=kit_lot or None,
            kit_expiry=expiry,
            equipment=equipment or None,
        )
    except (ServiceError, PermissionDenied) as error:
        flash(request, error.message, "error")
        return RedirectResponse("/app/lab", status_code=303)

    return RedirectResponse(f"/app/lab/runs/{run.id}", status_code=303)


@router.get("/runs/{run_id}")
def run_detail(
    run_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    run = db.scalars(
        select(LabRun).where(
            LabRun.id == run_id, LabRun.facility_id == principal.facility_id
        )
    ).first()

    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    actor = _actor(principal, request)

    # Donations still missing THIS run's marker — the plate's candidate samples.
    candidates = [
        row for row in service.pending(db, actor) if run.test_code in row.outstanding
    ]

    recorded = db.execute(
        select(
            DonationTest.donation_id,
            DonationTest.result,
            DonationTest.is_reactive,
            Donation.din,
            Donor.donor_code,
        )
        .join(Donation, Donation.id == DonationTest.donation_id)
        .join(Donor, Donor.id == Donation.donor_id)
        .where(DonationTest.lab_run_id == run.id)
        .order_by(Donation.collected_at)
    ).all()

    lang = current_lang(request)

    return _page(
        request,
        principal,
        db,
        template="app/lab_run.html",
        context={
            "run": run,
            "candidates": candidates,
            "recorded": recorded,
            "reactive_count": sum(1 for row in recorded if row.is_reactive),
        },
        page_title=run.run_code,
        breadcrumbs=[
            {"label": t("nav.lab", language=lang), "url": "/app/lab"},
            {"label": run.run_code, "url": f"/app/lab/runs/{run.id}"},
        ],
    )


@router.post("/runs/{run_id}/results")
async def record_results(
    run_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    """Record a plate.

    Read from the raw form because the sample list is whatever was on the plate,
    not a fixed set of named parameters.
    """

    form = await request.form()

    results = {
        key[len("result_") :]: value
        for key, value in form.items()
        if key.startswith("result_") and value
    }

    if not results:
        flash(
            request,
            t("ops.no_results_entered", language=current_lang(request)),
            "error",
        )
        return RedirectResponse(f"/app/lab/runs/{run_id}", status_code=303)

    try:
        outcome = service.record_results(
            db, _actor(principal, request), run_id=run_id, results=results
        )
    except (ServiceError, PermissionDenied) as error:
        flash(request, error.message, "error")
        return RedirectResponse(f"/app/lab/runs/{run_id}", status_code=303)

    if outcome["reactive"]:
        flash(
            request,
            t(
                "ops.reactive_results_recorded_flash",
                language=current_lang(request),
                recorded=outcome["recorded"],
                reactive=outcome["reactive"],
            ),
            "warning",
        )
    else:
        flash(
            request,
            t(
                "ops.results_recorded_flash",
                language=current_lang(request),
                count=outcome["recorded"],
            ),
            "success",
        )

    return RedirectResponse(f"/app/lab/runs/{run_id}", status_code=303)


@router.post("/runs/{run_id}/controls")
def record_controls(
    run_id: str,
    request: Request,
    controls_valid: str = Form("1"),
    note: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    valid = controls_valid == "1"

    try:
        service.record_controls(
            db,
            _actor(principal, request),
            run_id=run_id,
            controls_valid=valid,
            note=note or None,
        )
    except (ServiceError, PermissionDenied) as error:
        flash(request, error.message, "error")
        return RedirectResponse(f"/app/lab/runs/{run_id}", status_code=303)

    if valid:
        flash(
            request,
            t("ops.controls_valid_flash", language=current_lang(request)),
            "success",
        )
    else:
        flash(
            request,
            t("ops.controls_invalidated_flash", language=current_lang(request)),
            "warning",
        )

    return RedirectResponse(f"/app/lab/runs/{run_id}", status_code=303)


@router.post("/release/{donation_id}")
def release(
    donation_id: str,
    request: Request,
    note: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    try:
        donation = service.release(
            db, _actor(principal, request), donation_id=donation_id, note=note or None
        )
    except (ServiceError, PermissionDenied) as error:
        flash(request, error.message, "error")
        return RedirectResponse("/app/lab", status_code=303)

    flash(
        request,
        t(
            "ops.unit_released_flash",
            language=current_lang(request),
            din=donation.din,
        ),
        "success",
    )

    return RedirectResponse("/app/lab", status_code=303)


@router.post("/confirm/{donation_id}")
def confirm(
    donation_id: str,
    request: Request,
    marker: str = Form(...),
    confirmed: str = Form("0"),
    note: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    try:
        service.record_confirmation(
            db,
            _actor(principal, request),
            donation_id=donation_id,
            marker=marker,
            confirmed=confirmed == "1",
            note=note or None,
        )
    except (ServiceError, PermissionDenied) as error:
        flash(request, error.message, "error")
        return RedirectResponse("/app/lab", status_code=303)

    if confirmed == "1":
        flash(
            request,
            t("ops.confirmed_positive_flash", language=current_lang(request)),
            "warning",
        )
    else:
        flash(
            request,
            t("ops.not_confirmed_flash", language=current_lang(request)),
            "info",
        )

    return RedirectResponse("/app/lab", status_code=303)
