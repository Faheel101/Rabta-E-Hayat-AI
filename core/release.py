"""Stable release-acceptance contract shared by the UI and operator tools.

The product has many tests and screens.  This module gives the release process
one canonical vocabulary for the people, hand-offs, operating models, and
workflow evidence that must be present before a demonstration is accepted.
It deliberately contains no passwords and performs no database work.
"""

from __future__ import annotations

from dataclasses import dataclass


ACCEPTANCE_CONTRACT_VERSION = "15.0"


@dataclass(frozen=True)
class AcceptanceIdentity:
    code: str
    role: str
    role_key: str
    name: str
    email: str
    start_path: str
    workflow_ids: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    denied_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowDomain:
    code: str
    title_key: str
    owner_codes: tuple[str, ...]
    workflow_ids: tuple[str, ...]
    delta_key: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class OperatingModeProof:
    code: str
    title_key: str
    facility_code: str
    account_email: str
    proof_key: str


COMMON_DENIED_CLINICAL = (
    "/app/donors",
    "/app/sessions",
    "/app/lab",
    "/app/processing",
    "/app/inventory",
    "/app/requests",
    "/app/signoff",
)

COMMON_DENIED_PLANNING = (
    "/insights/command-centre",
    "/insights/forecast",
    "/insights/expiry-rescue",
    "/insights/transfer-plan",
    "/insights/simulator",
    "/insights/alerts",
    "/insights/facilities",
    "/insights/analytics",
    "/showcase",
)


ACCEPTANCE_IDENTITIES = (
    AcceptanceIdentity(
        code="PHLEBOTOMY",
        role="PHLEBOTOMIST",
        role_key="role.phlebotomist",
        name="Nasreen Bibi",
        email="n.bibi@punjab-teaching.rabta.pk",
        start_path="/app/sessions",
        workflow_ids=("PH-01", "PH-02", "PH-03", "PH-04", "PH-05"),
        allowed_paths=(
            "/app/dashboard",
            "/app/getting-started",
            "/ai",
            "/app/donors",
            "/app/sessions",
        ),
        denied_paths=(
            "/app/lab",
            "/app/processing",
            "/app/inventory",
            "/app/requests",
            "/app/signoff",
            *COMMON_DENIED_PLANNING,
            "/admin",
            "/admin/ai",
            "/admin/release",
            "/data",
        ),
    ),
    AcceptanceIdentity(
        code="LAB_A",
        role="LAB_TECHNOLOGIST",
        role_key="role.lab_technologist",
        name="Rizwan Aslam",
        email="r.aslam@punjab-teaching.rabta.pk",
        start_path="/app/processing",
        workflow_ids=("LAB-01", "LAB-02", "LAB-04"),
        allowed_paths=(
            "/app/dashboard",
            "/app/getting-started",
            "/ai",
            "/app/lab",
            "/app/processing",
            "/app/inventory",
        ),
        denied_paths=(
            "/app/donors",
            "/app/sessions",
            "/app/requests",
            "/app/signoff",
            *COMMON_DENIED_PLANNING,
            "/admin",
            "/admin/ai",
            "/admin/release",
            "/data",
        ),
    ),
    AcceptanceIdentity(
        code="LAB_B",
        role="LAB_TECHNOLOGIST",
        role_key="role.lab_technologist",
        name="Farah Noor",
        email="f.noor@punjab-teaching.rabta.pk",
        start_path="/app/lab",
        workflow_ids=("LAB-03",),
        allowed_paths=(
            "/app/dashboard",
            "/app/getting-started",
            "/ai",
            "/app/lab",
            "/app/processing",
            "/app/inventory",
        ),
        denied_paths=(
            "/app/donors",
            "/app/sessions",
            "/app/requests",
            "/app/signoff",
            *COMMON_DENIED_PLANNING,
            "/admin",
            "/admin/ai",
            "/admin/release",
            "/data",
        ),
    ),
    AcceptanceIdentity(
        code="BLOOD_BANK",
        role="BLOOD_BANK_OFFICER",
        role_key="role.bbo",
        name="Dr. Ahmed Raza",
        email="dr.ahmed@punjab-teaching.rabta.pk",
        start_path="/app/requests",
        workflow_ids=(
            "BBO-01",
            "BBO-02",
            "BBO-03",
            "BBO-04",
            "BBO-05",
            "BBO-06",
            "BBO-07",
        ),
        allowed_paths=(
            "/app/dashboard",
            "/app/getting-started",
            "/ai",
            "/app/donors",
            "/app/sessions",
            "/app/lab",
            "/app/processing",
            "/app/inventory",
            "/app/requests",
            "/app/signoff",
            "/insights/facilities",
            "/insights/analytics",
        ),
        denied_paths=("/admin", "/admin/ai", "/admin/release", "/data"),
    ),
    AcceptanceIdentity(
        code="NETWORK",
        role="RBC_COORDINATOR",
        role_key="role.rbc_coordinator",
        name="Sadia Fatima",
        email="s.fatima@punjab-teaching.rabta.pk",
        start_path="/insights/command-centre",
        workflow_ids=("RBC-01", "RBC-02", "RBC-03", "RBC-04"),
        allowed_paths=(
            "/app/dashboard",
            "/app/getting-started",
            "/ai",
            "/app/donors",
            "/app/sessions",
            "/app/lab",
            "/app/processing",
            "/app/inventory",
            "/app/requests",
            "/app/signoff",
            "/insights/command-centre",
            "/insights/forecast",
            "/insights/expiry-rescue",
            "/insights/transfer-plan",
            "/insights/simulator",
            "/insights/alerts",
            "/insights/facilities",
            "/insights/analytics",
            "/showcase",
            "/data",
        ),
        denied_paths=("/admin", "/admin/ai", "/admin/onboarding", "/admin/release"),
    ),
    AcceptanceIdentity(
        code="EMERGENCY",
        role="EMERGENCY_CONTROLLER",
        role_key="role.emergency_controller",
        name="Provincial Emergency Cell",
        email="control.room@south-punjab-dhq.rabta.pk",
        start_path="/insights/simulator",
        workflow_ids=("EC-01", "EC-02", "EC-03"),
        allowed_paths=(
            "/insights/command-centre",
            "/app/getting-started",
            "/ai",
            "/insights/simulator",
            "/insights/alerts",
            "/insights/transfer-plan",
            "/insights/facilities",
            "/showcase",
        ),
        denied_paths=(
            *COMMON_DENIED_CLINICAL,
            "/insights/analytics",
            "/admin",
            "/admin/ai",
            "/admin/release",
            "/data",
        ),
    ),
    AcceptanceIdentity(
        code="PROVINCE",
        role="PROVINCIAL_ADMIN",
        role_key="role.provincial_admin",
        name="Dr. Tariq Mahmood",
        email="dr.tariq@south-punjab-dhq.rabta.pk",
        start_path="/insights/command-centre",
        workflow_ids=("PA-01", "PA-02", "PA-03"),
        allowed_paths=(
            "/app/dashboard",
            "/app/getting-started",
            "/ai",
            "/insights/command-centre",
            "/insights/forecast",
            "/insights/expiry-rescue",
            "/insights/transfer-plan",
            "/insights/simulator",
            "/insights/alerts",
            "/insights/facilities",
            "/insights/analytics",
            "/showcase",
            "/admin",
            "/admin/ai",
            "/data",
        ),
        denied_paths=("/admin/onboarding", "/admin/release"),
    ),
    AcceptanceIdentity(
        code="SYSTEM",
        role="SYSTEM_ADMIN",
        role_key="role.system_admin",
        name="System Administrator",
        email="admin@punjab-teaching.rabta.pk",
        start_path="/admin/release",
        workflow_ids=("SA-01", "SA-02", "SA-03", "SA-04", "SA-05"),
        allowed_paths=(
            "/app/dashboard",
            "/app/getting-started",
            "/ai",
            "/insights/command-centre",
            "/insights/alerts",
            "/insights/facilities",
            "/insights/analytics",
            "/admin",
            "/admin/ai",
            "/admin/onboarding",
            "/admin/onboarding/new",
            "/admin/release",
            "/showcase",
            "/data",
        ),
        denied_paths=COMMON_DENIED_CLINICAL,
    ),
)


WORKFLOW_DOMAINS = (
    WorkflowDomain(
        "collection",
        "release.workflow_collection",
        ("PHLEBOTOMY",),
        ("PH-01", "PH-02", "PH-03", "PH-04", "PH-05"),
        "release.delta_collection",
        (
            "tests/clinical/test_sessions_and_signoff.py",
            "tests/clinical/test_screening_service.py",
            "tests/clinical/test_screening_drafts.py",
        ),
    ),
    WorkflowDomain(
        "clinical_signoff",
        "release.workflow_signoff",
        ("BLOOD_BANK",),
        ("BBO-01",),
        "release.delta_signoff",
        ("tests/clinical/test_sessions_and_signoff.py",),
    ),
    WorkflowDomain(
        "laboratory",
        "release.workflow_laboratory",
        ("LAB_A", "LAB_B"),
        ("LAB-01", "LAB-02", "LAB-03", "LAB-04"),
        "release.delta_laboratory",
        (
            "tests/clinical/test_processing.py",
            "tests/clinical/test_lab_release.py",
        ),
    ),
    WorkflowDomain(
        "patient",
        "release.workflow_patient",
        ("BLOOD_BANK",),
        ("BBO-02", "BBO-03", "BBO-04", "BBO-05", "BBO-06", "BBO-07"),
        "release.delta_patient",
        ("tests/clinical/test_request_to_transfusion.py",),
    ),
    WorkflowDomain(
        "network",
        "release.workflow_network",
        ("NETWORK",),
        ("RBC-01", "RBC-02", "RBC-03", "RBC-04"),
        "release.delta_network",
        (
            "tests/clinical/test_transfer_lifecycle.py",
            "tests/clinical/test_integrations.py",
        ),
    ),
    WorkflowDomain(
        "emergency",
        "release.workflow_emergency",
        ("EMERGENCY",),
        ("EC-01", "EC-02", "EC-03"),
        "release.delta_emergency",
        ("tests/clinical/test_emergency_and_alerts.py",),
    ),
    WorkflowDomain(
        "governance",
        "release.workflow_governance",
        ("PROVINCE",),
        ("PA-01", "PA-02", "PA-03"),
        "release.delta_governance",
        (
            "tests/clinical/test_governance_users.py",
            "tests/web/test_sprint10_uat.py",
        ),
    ),
    WorkflowDomain(
        "platform",
        "release.workflow_platform",
        ("SYSTEM",),
        ("SA-01", "SA-02", "SA-03", "SA-04", "SA-05"),
        "release.delta_platform",
        (
            "tests/clinical/test_network_onboarding.py",
            "tests/release/test_release_hardening.py",
            "tests/web/test_network_onboarding_web.py",
        ),
    ),
)


OPERATING_MODE_PROOFS = (
    OperatingModeProof(
        "standalone_private",
        "release.mode_standalone_private",
        "SHKAT_LAHORE",
        "a.hussain@shaukat-khanum.rabta.pk",
        "release.mode_standalone_private_proof",
    ),
    OperatingModeProof(
        "standalone_shared",
        "release.mode_standalone_shared",
        "CHILDREN_LAHORE",
        "dr.zainab@children-trust.rabta.pk",
        "release.mode_standalone_shared_proof",
    ),
    OperatingModeProof(
        "hospital_group",
        "release.mode_hospital_group",
        "JINNAH_LAHORE",
        "s.fatima@punjab-teaching.rabta.pk",
        "release.mode_hospital_group_proof",
    ),
    OperatingModeProof(
        "rbc_network",
        "release.mode_rbc_network",
        "RBC_LAHORE",
        "dr.khan@rbc-punjab-north.rabta.pk",
        "release.mode_rbc_network_proof",
    ),
    OperatingModeProof(
        "province",
        "release.mode_province",
        "DHQ_DGKHAN",
        "dr.tariq@south-punjab-dhq.rabta.pk",
        "release.mode_province_proof",
    ),
)


def unique_roles() -> set[str]:
    return {identity.role for identity in ACCEPTANCE_IDENTITIES}


def workflow_ids() -> set[str]:
    return {
        workflow_id
        for domain in WORKFLOW_DOMAINS
        for workflow_id in domain.workflow_ids
    }
