"""Live, tenant-scoped control page for the four-minute hackathon story.

This is not a slide deck pasted into the application.  Every proof point is
queried from the same operational database and quality run that powers the
working surfaces linked from each chapter.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import Permission, can, can_open_page
from db.models import (
    Alert,
    BloodRequest,
    BloodUnit,
    Donation,
    Donor,
    ExpiryRescue,
    ForecastRunSummary,
    SimulationRun,
    Transfer,
)
from db.readiness import readiness_report
from i18n.t import t
from web.deps import Principal, get_db, require_principal
from web.routers.facility import ENABLED_NAV, nav_counts
from web.templating import current_lang, render

router = APIRouter(prefix="/showcase")

ACTIVE_REQUESTS = ("PENDING", "CROSSMATCHED", "PARTIAL", "ISSUED")
OPEN_ALERTS = ("OPEN", "ACKNOWLEDGED", "ESCALATED")

CHAPTERS = (
    {
        "number": "01",
        "duration": "1:05",
        "title": "showcase.chapter_operations",
        "body": "showcase.chapter_operations_body",
        "proof": "showcase.chapter_operations_proof",
        "url": "/app/dashboard",
        "action": "showcase.open_operations",
        "tone": "brand",
        "page_key": "dashboard",
    },
    {
        "number": "02",
        "duration": "0:50",
        "title": "showcase.chapter_intelligence",
        "body": "showcase.chapter_intelligence_body",
        "proof": "showcase.chapter_intelligence_proof",
        "url": "/insights/command-centre",
        "action": "showcase.open_command",
        "tone": "info",
        "page_key": "command_centre",
    },
    {
        "number": "03",
        "duration": "0:45",
        "title": "showcase.chapter_transfer",
        "body": "showcase.chapter_transfer_body",
        "proof": "showcase.chapter_transfer_proof",
        "url": "/insights/transfer-plan",
        "action": "showcase.open_transfers",
        "tone": "safe",
        "page_key": "transfers",
    },
    {
        "number": "04",
        "duration": "0:50",
        "title": "showcase.chapter_emergency",
        "body": "showcase.chapter_emergency_body",
        "proof": "showcase.chapter_emergency_proof",
        "url": "/insights/simulator",
        "action": "showcase.open_simulator",
        "tone": "warn",
        "page_key": "simulator",
    },
    {
        "number": "05",
        "duration": "0:30",
        "title": "showcase.chapter_governance",
        "body": "showcase.chapter_governance_body",
        "proof": "showcase.chapter_governance_proof",
        "url": "/data",
        "action": "showcase.open_data",
        "tone": "neutral",
        "page_key": "data",
    },
)


def _count(db: Session, model, *criteria) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*criteria)) or 0)


def _quality(db: Session) -> dict:
    row = db.scalar(
        select(ForecastRunSummary).order_by(ForecastRunSummary.generated_at.desc())
    )

    if row is None:
        return {"available": False, "gates_passed": 0, "gates_total": 4}

    detail = row.metrics_json or {}
    decision_wape = detail.get("wape_component_grain_7d")
    beats = row.pct_series_beating_naive
    coverage = row.picp_p10_p90
    recall = row.shortage_detection_recall_3d
    gates = {
        "decision_wape": decision_wape is not None and decision_wape <= 0.25,
        "beats_naive": beats is not None and beats >= 80.0,
        "coverage": coverage is not None and 0.70 <= coverage <= 0.90,
        "recall": recall is not None and recall >= 0.75,
    }

    return {
        "available": True,
        "decision_wape": decision_wape,
        "group_wape": row.wape_dense_7d,
        "noise_floor": row.noise_floor_dense_7d,
        "beats_naive": beats,
        "coverage": coverage,
        "recall": recall,
        "fallbacks": row.series_fallback,
        "series": row.series_total,
        "gates": gates,
        "gates_passed": sum(gates.values()),
        "gates_total": len(gates),
    }


@router.get("")
def showcase(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    if principal.role in {"PHLEBOTOMIST", "LAB_TECHNOLOGIST"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This role does not use planning intelligence",
        )

    facility_ids = principal.scope_facility_ids
    organization_ids = {
        facility.organization_id
        for facility in principal.scope_facilities
        if facility.organization_id
    }
    ready, readiness = readiness_report()

    stats = {
        # Donor identity remains tenant-owned; the showcase headline preserves
        # that trust boundary even when aggregate planning scope is provincial.
        "donors": _count(db, Donor, Donor.organization_id == principal.organization_id),
        "donations": _count(db, Donation, Donation.facility_id.in_(facility_ids)),
        "available_units": _count(
            db,
            BloodUnit,
            BloodUnit.facility_id.in_(facility_ids),
            BloodUnit.status == "AVAILABLE",
            BloodUnit.screening_status == "PASSED",
        ),
        "active_requests": _count(
            db,
            BloodRequest,
            BloodRequest.facility_id.in_(facility_ids),
            BloodRequest.status.in_(ACTIVE_REQUESTS),
        ),
        "rescue_units": _count(
            db,
            ExpiryRescue,
            ExpiryRescue.facility_id.in_(facility_ids),
            ExpiryRescue.rescue_tier.in_(("ACT_NOW", "WATCH")),
        ),
        "transfers": _count(
            db,
            Transfer,
            or_(
                Transfer.from_facility_id.in_(facility_ids),
                Transfer.to_facility_id.in_(facility_ids),
            ),
        ),
        "open_alerts": _count(
            db,
            Alert,
            Alert.organization_id.in_(organization_ids),
            Alert.status.in_(OPEN_ALERTS),
        ),
        "simulations": _count(
            db,
            SimulationRun,
            SimulationRun.organization_id.in_(organization_ids),
            SimulationRun.status == "COMPLETED",
        ),
    }

    quality = _quality(db)
    subject = principal.role_subject()
    chapters = []
    for chapter in CHAPTERS:
        available = (
            chapter["page_key"] == "dashboard"
            or can_open_page(subject, chapter["page_key"])
        )
        if chapter["page_key"] == "data":
            available = available and can(subject, Permission.MANAGE_INTEGRATIONS)
        chapters.append({**chapter, "available": available})

    release_checks = {
        "database": readiness.get("database") == "ok",
        "schema": readiness.get("schema") == "ok",
        "forecast": quality["available"],
        "data": bool(stats["donors"] and stats["available_units"]),
        "journey": all(chapter["available"] for chapter in chapters),
        "activity": bool(stats["active_requests"] and stats["simulations"]),
    }

    lang = current_lang(request)

    return render(
        request,
        "insights/showcase.html",
        {
            "chapters": chapters,
            "stats": stats,
            "quality": quality,
            "release_ready": ready and all(release_checks.values()),
            "release_checks": release_checks,
        },
        principal=principal,
        db=db,
        page_title=t("showcase.title", language=lang),
        nav_counts=nav_counts(db, principal),
        enabled_nav=ENABLED_NAV,
    )
