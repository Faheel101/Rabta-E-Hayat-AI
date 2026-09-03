"""Component processing: the separation bench.

The worklist is ordered by which component window closes first rather than by
age, because the question a technologist is answering is "what will I lose next",
not "what arrived first". A bag five hours old with a platelet still to come is
more urgent than one collected yesterday that can only yield red cells now.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Permission
from db.models import ComponentProduction, Donation, Donor
from i18n.t import t
from services import processing as service
from services.audit import Actor, PermissionDenied, ServiceError
from web.deps import Principal, get_db, require_permission, require_principal
from web.routers.facility import ENABLED_NAV, nav_counts
from web.templating import current_lang, flash, render

router = APIRouter(
    prefix="/app/processing",
    dependencies=[Depends(require_permission(Permission.PROCESS_COMPONENTS))],
)


def _actor(principal: Principal, request: Request) -> Actor:
    return Actor.from_principal(principal, request)


@router.get("")
def bench(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    actor = _actor(principal, request)

    pending = service.pending(db, actor)

    # Per-bag window detail, so the page can show what is still makeable and
    # what has already been lost to the clock.
    rows = []

    for item in pending:
        windows = {
            code: service.window_status(code, item.collected_at)
            for code in item.expected
        }
        rows.append({"bag": item, "windows": windows})

    recent = db.execute(
        select(
            ComponentProduction.id,
            ComponentProduction.produced_at,
            ComponentProduction.recipe_code,
            ComponentProduction.units_expected,
            ComponentProduction.units_produced,
            ComponentProduction.loss_reasons,
            ComponentProduction.produced_by,
            ComponentProduction.minutes_from_collection,
            Donation.din,
        )
        .join(Donation, Donation.id == ComponentProduction.donation_id)
        .where(
            ComponentProduction.facility_id == principal.facility_id,
            ComponentProduction.produced_by.is_not(None),
        )
        .order_by(ComponentProduction.produced_at.desc())
        .limit(20)
    ).all()

    lang = current_lang(request)

    return render(
        request,
        principal=principal,
        db=db,
        nav_counts=nav_counts(db, principal),
        enabled_nav=ENABLED_NAV,
        template="app/processing.html",
        context={
            "rows": rows,
            "recent": recent,
            "summary": service.yield_summary(db, principal.facility_id),
            "loss_reasons": {
                code: service.loss_reasons_for(code)
                for code in ("PRBC", "PLT_RD", "FFP", "CRYO", "WB", "PLT_APH")
            },
            "can_process": principal.role
            in (
                "LAB_TECHNOLOGIST",
                "BLOOD_BANK_OFFICER",
                "RBC_COORDINATOR",
                "PROVINCIAL_ADMIN",
                "SYSTEM_ADMIN",
            ),
        },
        page_title=t("ops.component_processing_title", language=lang),
        breadcrumbs=[
            {"label": t("nav.dashboard", language=lang), "url": "/app/dashboard"},
            {"label": t("ops.component_processing_title", language=lang), "url": "/app/processing"},
        ],
    )


@router.post("/{donation_id}/separate")
async def separate(
    donation_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    """Separate a bag.

    Read from the raw form because which components are on offer depends on the
    bag and on how much of its window is left — restating them as named
    parameters would be a second place for that to drift.
    """

    form = await request.form()

    produce = [
        key[len("produce_") :] for key, value in form.items()
        if key.startswith("produce_") and value
    ]
    losses = {
        key[len("loss_") :]: value
        for key, value in form.items()
        if key.startswith("loss_") and value
    }

    try:
        record = service.separate(
            db,
            _actor(principal, request),
            donation_id=donation_id,
            produce=produce,
            losses=losses,
            notes=str(form.get("notes") or "") or None,
        )
    except (ServiceError, PermissionDenied) as error:
        flash(request, error.message, "error")
        return RedirectResponse("/app/processing", status_code=303)

    if record.units_produced < record.units_expected:
        flash(
            request,
            t(
                "ops.components_shortfall_flash",
                language=current_lang(request),
                produced=record.units_produced,
                expected=record.units_expected,
            ),
            "warning",
        )
    else:
        flash(
            request,
            t(
                "ops.components_separated_flash",
                language=current_lang(request),
                count=record.units_produced,
            ),
            "success",
        )

    return RedirectResponse("/app/processing", status_code=303)
