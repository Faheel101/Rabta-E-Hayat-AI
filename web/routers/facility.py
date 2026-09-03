"""Facility operations: the blood bank's own daily work.

Only modules that are actually built are registered in the navigation. A rail
full of links that 404 is worse than a short rail.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Permission
from db.models import (
    BloodGroup,
    BloodRequest,
    BloodUnit,
    Component,
    ExpiryRescue,
    MartDaysOfCover,
    Transfer,
)
from i18n.t import t
from services.common import DEMO_DATETIME
from web.deps import Principal, get_db, principal_can, require_principal
from web.guidance import build_role_guide, greeting_name
from web.templating import current_lang, render

router = APIRouter(prefix="/app")

# Grows as each module lands, so the rail never offers a dead link.
ENABLED_NAV = {
    "dashboard",
    "donors",
    "sessions",
    "lab",
    "processing",
    "signoff",
    "inventory",
    "requests",
    "command_centre",
    "forecast",
    "expiry",
    "plan",
    "simulator",
    "alerts",
    "facilities",
    "analytics",
    "admin",
    "showcase",
    "data",
    "getting_started",
    "ai_admin",
}

EXPIRY_SOON_HOURS = 72


def nav_counts(db: Session, principal: Principal) -> dict:
    facility_id = principal.facility_id

    if not facility_id:
        return {}

    counts = {}

    transfer_permissions = (
        Permission.APPROVE_TRANSFER_OUT,
        Permission.ACCEPT_TRANSFER_IN,
        Permission.RUN_OPTIMIZER,
    )
    if any(principal_can(principal, item) for item in transfer_permissions):
        counts["transfers_pending"] = db.scalar(
            select(func.count())
            .select_from(Transfer)
            .where(
                Transfer.status == "RECOMMENDED",
                (Transfer.from_facility_id == facility_id)
                | (Transfer.to_facility_id == facility_id),
            )
        ) or 0

    if principal_can(principal, Permission.MANAGE_CLINICAL_REQUEST):
        counts["requests_open"] = db.scalar(
            select(func.count())
            .select_from(BloodRequest)
            .where(
                BloodRequest.facility_id == facility_id,
                BloodRequest.status.in_(
                    ("PENDING", "CROSSMATCHED", "PARTIAL", "ISSUED")
                ),
            )
        ) or 0

    # The sign-off backlog, so the badge shows how many donors are held behind a
    # rule nobody has reviewed rather than requiring someone to open the page.
    if principal_can(principal, Permission.SIGN_OFF_DEFERRAL):
        from services import signoff

        scope = "organization" if principal.is_group_user else "facility"
        counts["signoff_pending"] = signoff.pending_count(
            db, principal.facility_ids(scope)
        )

    # Donations collected but not yet fully tested. A bag sitting here is shelf
    # life spent on nothing, so the badge is worth carrying.
    if principal_can(principal, Permission.PERFORM_TEST):
        from services import lab

        counts["lab_pending"] = lab.pending_count(db, principal.facility_id)

    # Released bags not yet separated. Urgent in a way the number alone does
    # not convey — a platelet window closes eight hours after collection.
    if principal_can(principal, Permission.PROCESS_COMPONENTS):
        from services import processing

        counts["processing_pending"] = processing.pending_count(
            db, principal.facility_id
        )

    if principal_can(principal, Permission.ACKNOWLEDGE_ALERT):
        from services.alert_service import open_alert_count

        counts["alerts_open"] = open_alert_count(
            db,
            principal.organization_id,
            principal.org_facility_ids,
        )

    if principal_can(principal, Permission.MANAGE_INTEGRATIONS):
        from db.models import ReconciliationIssue

        counts["data_issues"] = int(
            db.scalar(
                select(func.count())
                .select_from(ReconciliationIssue)
                .where(
                    ReconciliationIssue.organization_id
                    == principal.organization_id,
                    ReconciliationIssue.status == "OPEN",
                )
            )
            or 0
        )

    return counts


def _page(
    request: Request,
    principal: Principal,
    db: Session,
    *,
    nav_context: dict | None = None,
    **kwargs,
):
    return render(
        request,
        principal=principal,
        db=db,
        nav_counts=nav_context if nav_context is not None else nav_counts(db, principal),
        enabled_nav=ENABLED_NAV,
        **kwargs,
    )


@router.get("/dashboard")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    lang = current_lang(request)
    page_counts = nav_counts(db, principal)
    facility_id = principal.facility_id
    can_view_inventory = principal_can(principal, Permission.VIEW_LOCAL_INVENTORY)

    stock_rows = []
    expiring = []
    critical = []
    totals = {"units": 0, "expiring_72h": 0, "critical_series": 0, "at_risk": 0}

    if facility_id and can_view_inventory:
        stock_rows = db.execute(
            select(
                Component.code,
                Component.name_en,
                func.sum(MartDaysOfCover.units_available).label("units"),
                func.min(MartDaysOfCover.days_of_cover).label("min_cover"),
                func.sum(MartDaysOfCover.units_expiring_72h).label("expiring_72h"),
            )
            .join(Component, Component.id == MartDaysOfCover.component_id)
            .where(MartDaysOfCover.facility_id == facility_id)
            .group_by(Component.code, Component.name_en, Component.id)
            .order_by(Component.id)
        ).all()

        critical = db.execute(
            select(
                Component.code,
                BloodGroup.code,
                MartDaysOfCover.units_available,
                MartDaysOfCover.days_of_cover,
                MartDaysOfCover.reserve_floor,
                MartDaysOfCover.risk_bucket,
            )
            .join(Component, Component.id == MartDaysOfCover.component_id)
            .join(BloodGroup, BloodGroup.id == MartDaysOfCover.blood_group_id)
            .where(
                MartDaysOfCover.facility_id == facility_id,
                MartDaysOfCover.risk_bucket.in_(["CRITICAL", "WARNING"]),
            )
            .order_by(MartDaysOfCover.days_of_cover)
            .limit(8)
        ).all()

        cutoff = DEMO_DATETIME + timedelta(hours=EXPIRY_SOON_HOURS)

        expiring = db.execute(
            select(
                BloodUnit.din,
                Component.code,
                BloodGroup.code,
                BloodUnit.expires_at,
                ExpiryRescue.rescue_tier,
                ExpiryRescue.hours_to_deadline,
            )
            .join(Component, Component.id == BloodUnit.component_id)
            .join(BloodGroup, BloodGroup.id == BloodUnit.blood_group_id)
            .outerjoin(ExpiryRescue, ExpiryRescue.blood_unit_id == BloodUnit.id)
            .where(
                BloodUnit.facility_id == facility_id,
                BloodUnit.status == "AVAILABLE",
                BloodUnit.screening_status == "PASSED",
                BloodUnit.expires_at > DEMO_DATETIME,
                BloodUnit.expires_at <= cutoff,
            )
            .order_by(BloodUnit.expires_at)
            .limit(10)
        ).all()

        totals["units"] = sum(int(row.units or 0) for row in stock_rows)
        totals["expiring_72h"] = sum(int(row.expiring_72h or 0) for row in stock_rows)
        totals["critical_series"] = db.scalar(
            select(func.count())
            .select_from(MartDaysOfCover)
            .where(
                MartDaysOfCover.facility_id == facility_id,
                MartDaysOfCover.risk_bucket == "CRITICAL",
            )
        ) or 0
        totals["at_risk"] = db.scalar(
            select(func.count())
            .select_from(ExpiryRescue)
            .where(
                ExpiryRescue.facility_id == facility_id,
                ExpiryRescue.rescue_tier.in_(["ACT_NOW", "WATCH"]),
            )
        ) or 0

    return _page(
        request,
        principal,
        db,
        template="app/dashboard.html",
        context={
            "guide": build_role_guide(
                principal.role,
                page_counts,
                language=lang,
            ),
            "first_name": greeting_name(principal.display_name),
            "can_view_inventory": can_view_inventory,
            "stock_rows": stock_rows,
            "critical": critical,
            "expiring": expiring,
            "totals": totals,
            "expiry_window_hours": EXPIRY_SOON_HOURS,
        },
        nav_context=page_counts,
        page_title=t("nav.dashboard", language=lang),
    )
