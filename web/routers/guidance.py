"""Role-aware orientation and self-service workflow guidance."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from i18n.t import t
from services.audit import Actor
from services import onboarding_service
from web.deps import Principal, get_db, require_principal
from web.guidance import build_role_guide, greeting_name
from web.routers.auth import safe_redirect
from web.routers.facility import ENABLED_NAV, nav_counts
from web.templating import current_lang, flash, render


router = APIRouter(prefix="/app/getting-started")


@router.get("")
def getting_started(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    lang = current_lang(request)
    counts = nav_counts(db, principal)

    return render(
        request,
        "app/getting_started.html",
        {
            "guide": build_role_guide(principal.role, counts, language=lang),
            "first_name": greeting_name(principal.display_name),
        },
        principal=principal,
        db=db,
        nav_counts=counts,
        enabled_nav=ENABLED_NAV,
        page_title=t("onboarding.title", language=lang),
    )


@router.post("/complete")
def complete_orientation(
    request: Request,
    next: str = Form("/app/dashboard"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    onboarding_service.complete(
        db,
        Actor.from_principal(principal, request),
        principal.user,
    )
    flash(
        request,
        t("onboarding.completed_message", language=current_lang(request)),
        "safe",
    )

    return RedirectResponse(safe_redirect(next), status_code=303)


@router.post("/restart")
def restart_orientation(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    onboarding_service.restart(
        db,
        Actor.from_principal(principal, request),
        principal.user,
    )
    flash(
        request,
        t("onboarding.restarted_message", language=current_lang(request)),
        "info",
    )

    return RedirectResponse("/app/dashboard", status_code=303)
