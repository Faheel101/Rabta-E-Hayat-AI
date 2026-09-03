"""Read-only evidence for the release-candidate acceptance workspace."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import APP_VERSION, BASE_DIR, DEMO_DATE, SYNTHETIC_DATA, validate_runtime_config
from core.release import ACCEPTANCE_IDENTITIES, OPERATING_MODE_PROOFS, WORKFLOW_DOMAINS
from db.models import (
    Alert,
    AuditLog,
    BloodGroup,
    BloodRequest,
    BloodUnit,
    Component,
    Donation,
    Donor,
    Facility,
    ImportBatch,
    IntelligenceRefreshState,
    Organization,
    ReconciliationIssue,
    SimulationRun,
    Transfer,
    UserAccount,
)
from db.readiness import readiness_report
from scripts.release_check import REQUIRED_ASSETS


REQUIRED_COMPONENTS = {"WB", "PRBC", "PLT_RD", "PLT_APH", "FFP", "CRYO"}
REQUIRED_GROUPS = {"A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"}
ACTIVE_REQUESTS = {"PENDING", "CROSSMATCHED", "PARTIAL", "ISSUED"}
ACTIVE_ALERTS = {"OPEN", "ACKNOWLEDGED", "ESCALATED"}


def _count(db: Session, model, *criteria) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*criteria)) or 0)


def _mode_state(db: Session, code: str) -> tuple[bool, str]:
    proof = next(item for item in OPERATING_MODE_PROOFS if item.code == code)
    facility = db.scalar(select(Facility).where(Facility.code == proof.facility_code))
    user = db.scalar(select(UserAccount).where(UserAccount.email == proof.account_email))

    if facility is None or user is None or not facility.is_active or not user.is_active:
        return False, "reference facility or accountable access is missing"

    organization = db.get(Organization, facility.organization_id)
    if organization is None or not organization.is_active:
        return False, "organization is missing or inactive"

    if code == "standalone_private":
        passed = not facility.shares_inventory and not facility.shares_contact
    elif code == "standalone_shared":
        passed = bool(
            organization.org_type == "STANDALONE_HOSPITAL"
            and organization.network_opt_in
            and facility.shares_inventory
        )
    elif code == "hospital_group":
        owned = _count(
            db,
            Facility,
            Facility.organization_id == organization.id,
            Facility.is_active.is_(True),
        )
        passed = organization.org_type == "HOSPITAL_GROUP" and owned > 1
    elif code == "rbc_network":
        spokes = _count(
            db,
            Facility,
            Facility.parent_rbc_id == facility.id,
            Facility.is_active.is_(True),
        )
        passed = (
            organization.org_type == "RBC_OPERATOR"
            and facility.facility_type == "RBC"
            and spokes > 0
        )
    else:
        passed = (
            organization.org_type == "GOVT_PROGRAMME"
            and user.role == "PROVINCIAL_ADMIN"
        )

    return passed, f"{facility.name_en} · {organization.name_en}"


def acceptance_snapshot(db: Session) -> dict:
    """Return live, non-mutating evidence for a release review."""

    try:
        validate_runtime_config()
        configuration = "ok"
    except RuntimeError as error:
        configuration = str(error)
    database_ready, database = readiness_report()
    missing_assets = [name for name in REQUIRED_ASSETS if not (BASE_DIR / name).is_file()]
    release_ok = configuration == "ok" and database_ready and not missing_assets
    release_report = {
        "configuration": configuration,
        "database": database,
        "assets": "ok" if not missing_assets else {"missing": missing_assets},
        "note": "The full CLI gate also performs SQLite integrity verification.",
    }
    accounts = {
        row.email: row
        for row in db.scalars(
            select(UserAccount).where(
                UserAccount.email.in_(
                    [identity.email for identity in ACCEPTANCE_IDENTITIES]
                )
            )
        ).all()
    }
    identities = [
        {
            "contract": identity,
            "ready": bool(
                identity.email in accounts
                and accounts[identity.email].is_active
                and accounts[identity.email].role == identity.role
                and not accounts[identity.email].must_change_password
            ),
        }
        for identity in ACCEPTANCE_IDENTITIES
    ]
    role_access_ready = all(item["ready"] for item in identities)

    groups = set(db.scalars(select(BloodGroup.code)).all())
    components = set(db.scalars(select(Component.code)).all())
    reference_ready = REQUIRED_GROUPS.issubset(groups) and REQUIRED_COMPONENTS.issubset(
        components
    )

    refresh = db.get(IntelligenceRefreshState, "decision-intelligence")
    intelligence_ready = bool(
        refresh
        and refresh.status == "CLEAN"
        and refresh.completed_version == refresh.source_version
    )

    mode_rows = []
    for proof in OPERATING_MODE_PROOFS:
        passed, detail = _mode_state(db, proof.code)
        mode_rows.append({"contract": proof, "ready": passed, "detail": detail})

    two_person_facilities = int(
        db.scalar(
            select(func.count())
            .select_from(
                select(UserAccount.facility_id)
                .where(
                    UserAccount.role == "LAB_TECHNOLOGIST",
                    UserAccount.is_active.is_(True),
                    UserAccount.facility_id.is_not(None),
                )
                .group_by(UserAccount.facility_id)
                .having(func.count(UserAccount.id) >= 2)
                .subquery()
            )
        )
        or 0
    )

    counts = {
        "organizations": _count(db, Organization, Organization.is_active.is_(True)),
        "facilities": _count(db, Facility, Facility.is_active.is_(True)),
        "draft_facilities": _count(
            db,
            Facility,
            Facility.is_active.is_(False),
            Facility.onboarding_status == "DRAFT",
        ),
        "active_users": _count(db, UserAccount, UserAccount.is_active.is_(True)),
        "donors": _count(db, Donor),
        "donations": _count(db, Donation),
        "available_units": _count(
            db,
            BloodUnit,
            BloodUnit.status == "AVAILABLE",
            BloodUnit.screening_status == "PASSED",
        ),
        "open_requests": _count(db, BloodRequest, BloodRequest.status.in_(ACTIVE_REQUESTS)),
        "transfers": _count(db, Transfer),
        "active_alerts": _count(db, Alert, Alert.status.in_(ACTIVE_ALERTS)),
        "simulations": _count(db, SimulationRun, SimulationRun.status == "COMPLETED"),
        "imports": _count(db, ImportBatch),
        "open_reconciliation": _count(
            db, ReconciliationIssue, ReconciliationIssue.status == "OPEN"
        ),
        "audit_events": _count(db, AuditLog),
        "two_person_lab_facilities": two_person_facilities,
    }

    gates = (
        {
            "code": "runtime",
            "label_key": "release.gate_runtime",
            "detail_key": "release.gate_runtime_help",
            "ready": release_ok,
        },
        {
            "code": "synthetic",
            "label_key": "release.gate_synthetic",
            "detail_key": "release.gate_synthetic_help",
            "ready": SYNTHETIC_DATA,
        },
        {
            "code": "roles",
            "label_key": "release.gate_roles",
            "detail_key": "release.gate_roles_help",
            "ready": role_access_ready,
        },
        {
            "code": "reference",
            "label_key": "release.gate_reference",
            "detail_key": "release.gate_reference_help",
            "ready": reference_ready,
        },
        {
            "code": "segregation",
            "label_key": "release.gate_segregation",
            "detail_key": "release.gate_segregation_help",
            "ready": two_person_facilities > 0,
        },
        {
            "code": "intelligence",
            "label_key": "release.gate_intelligence",
            "detail_key": "release.gate_intelligence_help",
            "ready": intelligence_ready,
        },
        {
            "code": "modes",
            "label_key": "release.gate_modes",
            "detail_key": "release.gate_modes_help",
            "ready": all(row["ready"] for row in mode_rows),
        },
    )

    return {
        "version": APP_VERSION,
        "scenario_date": DEMO_DATE,
        "release_ready": all(gate["ready"] for gate in gates),
        "gates": gates,
        "release_report": release_report,
        "counts": counts,
        "identities": identities,
        "workflows": WORKFLOW_DOMAINS,
        "modes": mode_rows,
        "refresh": refresh,
    }
