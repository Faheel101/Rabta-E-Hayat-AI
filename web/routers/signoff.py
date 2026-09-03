"""The clinical sign-off queue for the seven contested deferral rules.

609 donors are currently held off the register by a config default that nobody
has reviewed. The queue exists so that is a decision somebody made rather than a
decision the configuration made silently.

Both limbs of each disagreement are shown, along with the source note explaining
why the rule is contested at all — a reviewer who only sees the answer the config
picked cannot weigh anything.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import Permission
from i18n.t import t
from services import signoff as service
from services.audit import Actor, PermissionDenied, ServiceError
from web.deps import Principal, get_db, require_permission, require_principal
from web.routers.facility import ENABLED_NAV, nav_counts
from web.templating import current_lang, flash, render

router = APIRouter(
    prefix="/app/signoff",
    dependencies=[Depends(require_permission(Permission.SIGN_OFF_DEFERRAL))],
)

# How each contested limb reads in plain language. The config stores a code; a
# reviewer needs the sentence.
LIMB_LABELS = {
    "PBTA_365_DAYS": "Punjab SOP — defer 12 months",
    "WHO_PERMANENT": "WHO — defer permanently",
    "PBTA_PERMANENT": "Punjab SOP — defer permanently",
    "WHO_2_YEARS_AFTER_CURE": "WHO — defer 2 years after documented cure",
    "WHO_12_MONTHS_WITH_MARKER_REENTRY": "WHO — 12 months, with marker re-entry testing",
    "TIMED_1095_DAYS": "Applied here — defer 3 years",
    "CONDITIONAL_UNTIL_WEANED": "Applied here — defer until weaning",
    "DELIVERY_PLUS_180": "Alternative — 6 months from delivery",
    "WHO_28_DAYS": "WHO — defer 28 days",
    "PBTA_70_MMHG": "Punjab SOP — diastolic floor 70 mmHg",
    "WHO_60_MMHG": "WHO — diastolic floor 60 mmHg",
}


def _actor(principal: Principal, request: Request) -> Actor:
    return Actor.from_principal(principal, request)


@router.get("")
def queue(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    actor = _actor(principal, request)
    cases = service.pending(db, actor)

    # Grouped by rule, because the reviewer is weighing a rule rather than a
    # person — the same argument settles every case under it.
    grouped: dict[str, list] = {}

    for case in cases:
        grouped.setdefault(case["reason_code"], []).append(case)

    lang = current_lang(request)

    return render(
        request,
        principal=principal,
        db=db,
        nav_counts=nav_counts(db, principal),
        enabled_nav=ENABLED_NAV,
        template="app/signoff.html",
        context={
            "grouped": sorted(
                grouped.items(), key=lambda item: -len(item[1])
            ),
            "total": len(cases),
            "rules": service.contested_rules(),
            "limb_labels": LIMB_LABELS,
        },
        page_title=t("ops.clinical_signoff_title", language=lang),
        breadcrumbs=[
            {"label": t("nav.dashboard", language=lang), "url": "/app/dashboard"},
            {"label": t("ops.clinical_signoff_title", language=lang), "url": "/app/signoff"},
        ],
    )


@router.post("/{deferral_id}/lift")
def lift(
    deferral_id: str,
    request: Request,
    reason: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    try:
        service.lift(
            db, _actor(principal, request), deferral_id=deferral_id, reason=reason
        )
        flash(
            request,
            t("ops.deferral_lifted_flash", language=current_lang(request)),
            "success",
        )
    except (ServiceError, PermissionDenied) as error:
        flash(request, error.message, "error")

    return RedirectResponse("/app/signoff", status_code=303)


@router.post("/{deferral_id}/uphold")
def uphold(
    deferral_id: str,
    request: Request,
    reason: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    try:
        service.uphold(
            db, _actor(principal, request), deferral_id=deferral_id, reason=reason
        )
        flash(
            request,
            t("ops.deferral_upheld_flash", language=current_lang(request)),
            "success",
        )
    except (ServiceError, PermissionDenied) as error:
        flash(request, error.message, "error")

    return RedirectResponse("/app/signoff", status_code=303)
