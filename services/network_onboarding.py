"""Permissioned, two-stage onboarding for organizations and facilities.

The onboarding boundary is deliberately transactional. A partially configured
blood bank must never leak into operational scope, forecasts, transfer plans or
clinical queues. The first transaction creates an inactive draft with its
storage, feed and first account. A separate activation transaction rechecks the
same readiness contract before making any of it operational.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Permission, Role
from core.policy import build_group_shares, expand_reserve_policy, facility_policy
from db.models import (
    BloodGroup,
    Facility,
    IntegrationFeed,
    Organization,
    StorageLocation,
    UserAccount,
    new_id,
)
from services.audit import Actor, ServiceError, audited, require
from web.security import hash_password


OPERATING_MODES = {
    "STANDALONE": "STANDALONE_HOSPITAL",
    "HOSPITAL_NETWORK": "HOSPITAL_GROUP",
    "RBC_NETWORK": "RBC_OPERATOR",
    "PROVINCIAL_PROGRAMME": "GOVT_PROGRAMME",
}
ORG_TYPE_TO_MODE = {value: key for key, value in OPERATING_MODES.items()}
FACILITY_TYPES = {
    "RBC",
    "TERTIARY_HOSPITAL",
    "SPECIALIST_CENTRE",
    "DHQ",
    "THQ",
}
INTEGRATION_MODES = {"SIMULATED", "CSV", "FHIR", "HL7", "API"}
FIRST_ACCOUNT_ROLES = {Role.BLOOD_BANK_OFFICER.value, Role.RBC_COORDINATOR.value}
RESERVE_COMPONENTS = {"WB", "PRBC", "PLT_RD", "PLT_APH", "FFP", "CRYO"}
REQUIRED_BLOOD_GROUPS = {"B+", "O+", "A+", "AB+", "O-", "B-", "A-", "AB-"}
CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,29}$")
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _clean(value: str | None, *, maximum: int = 255) -> str:
    return " ".join(str(value or "").strip().split())[:maximum]


def _required(value: str | None, field: str, label: str, *, maximum: int = 255) -> str:
    cleaned = _clean(value, maximum=maximum)
    if not cleaned:
        raise ServiceError("FIELD_REQUIRED", f"{label} is required.", field=field)
    return cleaned


def _code(value: str | None, field: str, label: str) -> str:
    cleaned = _clean(value, maximum=30).upper().replace("-", "_").replace(" ", "_")
    if not CODE_PATTERN.fullmatch(cleaned):
        raise ServiceError(
            "CODE_INVALID",
            f"{label} must start with a letter and use 3–30 letters, numbers or underscores.",
            field=field,
        )
    return cleaned


def validate_temporary_password(password: str) -> str:
    value = str(password or "")
    checks = (
        len(value) >= 12,
        any(character.islower() for character in value),
        any(character.isupper() for character in value),
        any(character.isdigit() for character in value),
        any(not character.isalnum() for character in value),
    )
    if not all(checks):
        raise ServiceError(
            "PASSWORD_WEAK",
            "Use at least 12 characters with upper- and lower-case letters, a number and a symbol.",
            field="temporary_password",
        )
    return value


def _authorize(actor: Actor) -> None:
    require(actor, Permission.MANAGE_NETWORK, "onboard organizations and facilities")


def _operating_mode(organization: Organization) -> str:
    configured = str((organization.settings_json or {}).get("operating_mode") or "")
    return configured if configured in OPERATING_MODES else ORG_TYPE_TO_MODE.get(
        organization.org_type, "STANDALONE"
    )


def _parent_rbc(
    db: Session,
    *,
    parent_rbc_id: str | None,
    province: str,
) -> Facility | None:
    if not parent_rbc_id:
        return None
    parent = db.scalar(
        select(Facility).where(
            Facility.id == parent_rbc_id,
            Facility.facility_type == "RBC",
            Facility.is_active.is_(True),
        )
    )
    if parent is None or parent.province.casefold() != province.casefold():
        raise ServiceError(
            "PARENT_RBC_INVALID",
            "Choose an active Regional Blood Centre in the same province.",
            field="parent_rbc_id",
        )
    return parent


def _storage_rows(
    facility_id: str,
    *,
    fridge_capacity: int,
    freezer_capacity: int,
    platelet_capacity: int,
) -> list[StorageLocation]:
    fridge = max(1, int(fridge_capacity))
    freezer = max(0, int(freezer_capacity))
    platelets = max(0, int(platelet_capacity))
    if fridge > 100_000 or freezer > 100_000 or platelets > 100_000:
        raise ServiceError(
            "CAPACITY_INVALID",
            "Storage capacity must be between 0 and 100,000 units.",
            field="fridge_capacity",
        )
    rows = [
        StorageLocation(
            id=new_id(),
            facility_id=facility_id,
            code="BLOOD_FRIDGE_01",
            name="Blood component refrigerator",
            location_type="FRIDGE",
            target_temp_min_c=2,
            target_temp_max_c=6,
            capacity_units=fridge,
        ),
        StorageLocation(
            id=new_id(),
            facility_id=facility_id,
            code="QUARANTINE_01",
            name="Quarantine refrigerator",
            location_type="QUARANTINE_FRIDGE",
            target_temp_min_c=2,
            target_temp_max_c=6,
            capacity_units=max(10, min(100, round(fridge * 0.1))),
            is_quarantine=True,
        ),
    ]
    if freezer:
        rows.append(
            StorageLocation(
                id=new_id(),
                facility_id=facility_id,
                code="PLASMA_FREEZER_01",
                name="Plasma freezer",
                location_type="FREEZER",
                target_temp_min_c=-30,
                target_temp_max_c=-18,
                capacity_units=freezer,
            )
        )
    if platelets:
        rows.append(
            StorageLocation(
                id=new_id(),
                facility_id=facility_id,
                code="PLATELET_AGITATOR_01",
                name="Platelet incubator and agitator",
                location_type="PLATELET_AGITATOR",
                target_temp_min_c=20,
                target_temp_max_c=24,
                capacity_units=platelets,
                has_agitator=True,
            )
        )
    return rows


def create_draft(
    db: Session,
    actor: Actor,
    *,
    organization_action: str,
    operating_mode: str,
    existing_organization_id: str | None,
    organization_code: str,
    organization_name_en: str,
    organization_name_ur: str | None,
    province: str,
    contact_email: str | None,
    contact_phone: str | None,
    facility_code: str,
    facility_name_en: str,
    facility_name_ur: str | None,
    facility_type: str,
    district: str,
    division: str | None,
    latitude: float,
    longitude: float,
    bed_count: int,
    parent_rbc_id: str | None,
    integration_mode: str,
    shares_inventory: bool,
    shares_contact: bool,
    network_response_sla_minutes: int,
    has_trauma_centre: bool,
    has_obgyn: bool,
    has_oncology: bool,
    has_thalassaemia_centre: bool,
    has_cardiac_surgery: bool,
    fridge_capacity: int,
    freezer_capacity: int,
    platelet_capacity: int,
    admin_name: str,
    admin_email: str,
    admin_role: str,
    temporary_password: str,
) -> Facility:
    """Create a complete but invisible onboarding draft in one transaction."""

    _authorize(actor)
    action = str(organization_action or "NEW").upper()
    mode = str(operating_mode or "").upper()
    if mode not in OPERATING_MODES:
        raise ServiceError("MODE_INVALID", "Choose a supported operating mode.", field="operating_mode")
    if action not in {"NEW", "EXISTING"}:
        raise ServiceError("ORG_ACTION_INVALID", "Choose a new or existing organization.")

    province_value = _required(province, "province", "Province", maximum=120)
    organization: Organization | None = None
    created_organization = False
    if action == "EXISTING":
        organization = db.get(Organization, existing_organization_id)
        if organization is None or not organization.is_active:
            raise ServiceError(
                "ORGANIZATION_NOT_FOUND",
                "Choose an active organization.",
                field="existing_organization_id",
            )
        mode = _operating_mode(organization)
        province_value = organization.province
    else:
        org_code = _code(organization_code, "organization_code", "Organization code")
        if db.scalar(select(Organization.id).where(func.upper(Organization.code) == org_code)):
            raise ServiceError("ORG_CODE_EXISTS", "That organization code is already in use.", field="organization_code")
        email = _clean(contact_email, maximum=255).lower() or None
        if email and not EMAIL_PATTERN.fullmatch(email):
            raise ServiceError("EMAIL_INVALID", "Enter a valid contact email.", field="contact_email")
        organization = Organization(
            id=new_id(),
            code=org_code,
            name_en=_required(organization_name_en, "organization_name_en", "Organization name"),
            name_ur=_clean(organization_name_ur) or None,
            org_type=OPERATING_MODES[mode],
            province=province_value,
            contact_email=email,
            contact_phone=_clean(contact_phone, maximum=40) or None,
            network_opt_in=mode != "STANDALONE",
            settings_json={
                "operating_mode": mode,
                "onboarding": {"status": "DRAFT"},
            },
            is_active=False,
        )
        created_organization = True

    assert organization is not None
    facility_code_value = _code(facility_code, "facility_code", "Facility code")
    if db.scalar(select(Facility.id).where(func.upper(Facility.code) == facility_code_value)):
        raise ServiceError("FACILITY_CODE_EXISTS", "That facility code is already in use.", field="facility_code")
    facility_type_value = str(facility_type or "").upper()
    if facility_type_value not in FACILITY_TYPES:
        raise ServiceError("FACILITY_TYPE_INVALID", "Choose a supported facility type.", field="facility_type")
    blood_groups = list(db.scalars(select(BloodGroup).order_by(BloodGroup.code)).all())
    if not REQUIRED_BLOOD_GROUPS.issubset({group.code for group in blood_groups}):
        raise ServiceError(
            "REFERENCE_DATA_MISSING",
            "All eight blood-group references must be configured before onboarding.",
        )
    component_capacity, reserve_totals, operating_hours = facility_policy(
        facility_type_value
    )
    reserve_policy = expand_reserve_policy(
        reserve_totals,
        facility_type_value,
        build_group_shares(blood_groups),
    )
    integration = str(integration_mode or "").upper()
    if integration not in INTEGRATION_MODES:
        raise ServiceError("INTEGRATION_MODE_INVALID", "Choose a supported integration mode.", field="integration_mode")
    try:
        latitude_value = float(latitude)
        longitude_value = float(longitude)
        beds = int(bed_count)
        sla = int(network_response_sla_minutes)
    except (TypeError, ValueError) as exc:
        raise ServiceError("NUMBER_INVALID", "Check the numeric facility fields.") from exc
    if not 23 <= latitude_value <= 38 or not 60 <= longitude_value <= 78:
        raise ServiceError(
            "COORDINATES_INVALID",
            "Coordinates must identify a location in Pakistan.",
            field="latitude",
        )
    if not 0 <= beds <= 10_000:
        raise ServiceError("BED_COUNT_INVALID", "Bed count must be between 0 and 10,000.", field="bed_count")
    if not 5 <= sla <= 1_440:
        raise ServiceError("SLA_INVALID", "Response SLA must be between 5 and 1,440 minutes.", field="network_response_sla_minutes")

    parent = _parent_rbc(db, parent_rbc_id=parent_rbc_id, province=province_value)
    if mode == "RBC_NETWORK" and facility_type_value != "RBC" and parent is None:
        raise ServiceError(
            "PARENT_RBC_REQUIRED",
            "A spoke facility in an RBC network requires a parent RBC.",
            field="parent_rbc_id",
        )
    if mode == "STANDALONE":
        parent = None
        shares_inventory = False
        shares_contact = False

    email = _clean(admin_email, maximum=255).lower()
    if not EMAIL_PATTERN.fullmatch(email):
        raise ServiceError("EMAIL_INVALID", "Enter a valid account email.", field="admin_email")
    if db.scalar(select(UserAccount.id).where(func.lower(UserAccount.email) == email)):
        raise ServiceError("USER_EMAIL_EXISTS", "That sign-in email is already in use.", field="admin_email")
    account_role = str(admin_role or "").upper()
    if account_role not in FIRST_ACCOUNT_ROLES:
        raise ServiceError("ROLE_INVALID", "Choose a facility officer or RBC coordinator.", field="admin_role")
    password = validate_temporary_password(temporary_password)

    facility = Facility(
        id=new_id(),
        code=facility_code_value,
        organization_id=organization.id,
        name_en=_required(facility_name_en, "facility_name_en", "Facility name"),
        name_ur=_clean(facility_name_ur) or None,
        facility_type=facility_type_value,
        shares_inventory=bool(shares_inventory),
        shares_contact=bool(shares_contact),
        network_response_sla_minutes=sla,
        parent_rbc_id=parent.id if parent else None,
        district=_required(district, "district", "District", maximum=120),
        division=_clean(division, maximum=120) or None,
        province=province_value,
        latitude=latitude_value,
        longitude=longitude_value,
        bed_count=beds,
        has_trauma_centre=bool(has_trauma_centre),
        has_obgyn=bool(has_obgyn),
        has_oncology=bool(has_oncology),
        has_thalassaemia_centre=bool(has_thalassaemia_centre),
        has_cardiac_surgery=bool(has_cardiac_surgery),
        storage_capacity_json=component_capacity,
        min_reserve_policy_json=reserve_policy,
        operating_hours_json=operating_hours,
        integration_mode=integration,
        onboarding_status="DRAFT",
        is_active=False,
    )
    storage = _storage_rows(
        facility.id,
        fridge_capacity=fridge_capacity,
        freezer_capacity=freezer_capacity,
        platelet_capacity=platelet_capacity,
    )
    feed = IntegrationFeed(
        id=new_id(),
        organization_id=organization.id,
        facility_id=facility.id,
        mode=integration,
        status="NEVER_SYNCED",
        capabilities_json={
            "unit_level": True,
            "aggregate_only": False,
            "supports_push": integration in {"FHIR", "HL7", "API", "SIMULATED"},
            "onboarding_ready": True,
        },
        config_json={"created_by": "guided_onboarding"},
    )
    first_user = UserAccount(
        id=new_id(),
        organization_id=organization.id,
        facility_id=facility.id,
        email=email,
        password_hash=hash_password(password),
        full_name=_required(admin_name, "admin_name", "Account holder name", maximum=160),
        role=account_role,
        preferences_json={
            "access_setup": {
                "source": "facility_onboarding",
                "facility_id": facility.id,
            }
        },
        is_active=False,
        must_change_password=True,
    )

    with audited(
        db,
        actor,
        "network_onboarding.draft.create",
        "facility",
        facility.id,
    ) as entry:
        if created_organization:
            db.add(organization)
            # These models intentionally avoid ORM relationships so tenant
            # boundaries stay explicit. Flush the parent before its children
            # rather than relying on relationship dependency ordering.
            db.flush()
        db.add(facility)
        db.flush()
        db.add_all(storage)
        db.add(feed)
        db.add(first_user)
        entry.on(
            facility,
            after={
                "organization_id": organization.id,
                "organization_created": created_organization,
                "operating_mode": mode,
                "facility_code": facility.code,
                "facility_type": facility.facility_type,
                "integration_mode": facility.integration_mode,
                "onboarding_status": facility.onboarding_status,
                "storage_locations": len(storage),
                "first_account_id": first_user.id,
            },
        )
    return facility


def _draft(db: Session, actor: Actor, facility_id: str) -> Facility:
    _authorize(actor)
    facility = db.get(Facility, facility_id)
    if facility is None or facility.onboarding_status != "DRAFT" or facility.is_active:
        raise ServiceError("DRAFT_NOT_FOUND", "Onboarding draft not found.")
    return facility


def readiness(db: Session, facility: Facility) -> dict:
    organization = db.get(Organization, facility.organization_id)
    feed = db.scalar(select(IntegrationFeed).where(IntegrationFeed.facility_id == facility.id))
    regular_storage = int(
        db.scalar(
            select(func.count()).select_from(StorageLocation).where(
                StorageLocation.facility_id == facility.id,
                StorageLocation.is_active.is_(True),
                StorageLocation.is_quarantine.is_(False),
                StorageLocation.capacity_units > 0,
            )
        )
        or 0
    )
    quarantine_storage = int(
        db.scalar(
            select(func.count()).select_from(StorageLocation).where(
                StorageLocation.facility_id == facility.id,
                StorageLocation.is_active.is_(True),
                StorageLocation.is_quarantine.is_(True),
            )
        )
        or 0
    )
    prepared_accounts = int(
        db.scalar(
            select(func.count()).select_from(UserAccount).where(
                UserAccount.facility_id == facility.id,
                UserAccount.role.in_(FIRST_ACCOUNT_ROLES),
            )
        )
        or 0
    )
    mode = _operating_mode(organization) if organization else "STANDALONE"
    network_ready = not (
        mode == "RBC_NETWORK"
        and facility.facility_type != "RBC"
        and not facility.parent_rbc_id
    )
    checks = [
        {
            "key": "profile",
            "passed": bool(
                organization
                and facility.code
                and facility.name_en
                and facility.district
                and facility.province
            ),
        },
        {"key": "storage", "passed": regular_storage > 0},
        {"key": "quarantine", "passed": quarantine_storage > 0},
        {
            "key": "reserve_policy",
            "passed": bool(
                RESERVE_COMPONENTS.issubset(facility.min_reserve_policy_json or {})
                and all(
                    isinstance(groups, dict)
                    and REQUIRED_BLOOD_GROUPS.issubset(groups)
                    for groups in (facility.min_reserve_policy_json or {}).values()
                )
            ),
        },
        {"key": "connection", "passed": bool(feed and feed.mode in INTEGRATION_MODES)},
        {"key": "access", "passed": prepared_accounts > 0},
        {"key": "network", "passed": network_ready},
    ]
    return {
        "ready": all(item["passed"] for item in checks),
        "checks": checks,
        "organization": organization,
        "feed": feed,
        "operating_mode": mode,
        "regular_storage": regular_storage,
        "quarantine_storage": quarantine_storage,
        "prepared_accounts": prepared_accounts,
    }


def list_drafts(db: Session, actor: Actor) -> list[dict]:
    _authorize(actor)
    facilities = list(
        db.scalars(
            select(Facility)
            .where(Facility.onboarding_status == "DRAFT", Facility.is_active.is_(False))
            .order_by(Facility.name_en)
        ).all()
    )
    return [{"facility": facility, **readiness(db, facility)} for facility in facilities]


def get_draft(db: Session, actor: Actor, facility_id: str) -> dict:
    facility = _draft(db, actor, facility_id)
    stores = list(
        db.scalars(
            select(StorageLocation)
            .where(StorageLocation.facility_id == facility.id)
            .order_by(StorageLocation.is_quarantine, StorageLocation.name)
        ).all()
    )
    accounts = list(
        db.scalars(
            select(UserAccount)
            .where(UserAccount.facility_id == facility.id)
            .order_by(UserAccount.full_name)
        ).all()
    )
    return {"facility": facility, "stores": stores, "accounts": accounts, **readiness(db, facility)}


def add_storage_location(
    db: Session,
    actor: Actor,
    facility_id: str,
    *,
    code: str,
    name: str,
    location_type: str,
    target_temp_min_c: float,
    target_temp_max_c: float,
    capacity_units: int,
    is_quarantine: bool,
    has_agitator: bool,
) -> StorageLocation:
    facility = _draft(db, actor, facility_id)
    code_value = _code(code, "code", "Storage code")
    if db.scalar(
        select(StorageLocation.id).where(
            StorageLocation.facility_id == facility.id,
            func.upper(StorageLocation.code) == code_value,
        )
    ):
        raise ServiceError("STORAGE_CODE_EXISTS", "That storage code is already in use.", field="code")
    minimum, maximum = float(target_temp_min_c), float(target_temp_max_c)
    capacity = int(capacity_units)
    if minimum >= maximum or not -90 <= minimum <= 30 or not -90 <= maximum <= 40:
        raise ServiceError("TEMPERATURE_RANGE_INVALID", "Enter a valid minimum and maximum temperature.")
    if not 1 <= capacity <= 100_000:
        raise ServiceError("CAPACITY_INVALID", "Capacity must be between 1 and 100,000 units.")
    record = StorageLocation(
        id=new_id(),
        facility_id=facility.id,
        code=code_value,
        name=_required(name, "name", "Storage name", maximum=120),
        location_type=_required(location_type, "location_type", "Storage type", maximum=30).upper(),
        target_temp_min_c=minimum,
        target_temp_max_c=maximum,
        capacity_units=capacity,
        is_quarantine=bool(is_quarantine),
        has_agitator=bool(has_agitator),
    )
    with audited(db, actor, "network_onboarding.storage.add", "storage_location", record.id) as entry:
        db.add(record)
        entry.on(
            record,
            after={
                "facility_id": facility.id,
                "code": record.code,
                "location_type": record.location_type,
                "capacity_units": record.capacity_units,
                "is_quarantine": record.is_quarantine,
            },
        )
    return record


def activate(db: Session, actor: Actor, facility_id: str) -> Facility:
    facility = _draft(db, actor, facility_id)
    state = readiness(db, facility)
    if not state["ready"]:
        failed = [item["key"] for item in state["checks"] if not item["passed"]]
        raise ServiceError(
            "READINESS_INCOMPLETE",
            "Complete every readiness check before activation: " + ", ".join(failed),
        )
    organization = state["organization"]
    now = datetime.now(timezone.utc)
    prepared_users = list(
        db.scalars(select(UserAccount).where(UserAccount.facility_id == facility.id)).all()
    )
    with audited(
        db,
        actor,
        "facility.onboarding.activate",
        "facility",
        facility.id,
    ) as entry:
        before = {"is_active": False, "onboarding_status": "DRAFT"}
        facility.is_active = True
        facility.onboarding_status = "ACTIVE"
        facility.activated_at = now
        facility.activated_by = actor.display_name
        if organization is not None:
            organization.is_active = True
            settings = dict(organization.settings_json or {})
            settings["onboarding"] = {
                "status": "ACTIVE",
                "activated_at": now.isoformat(),
            }
            organization.settings_json = settings
        activated_accounts = 0
        for user in prepared_users:
            setup = (user.preferences_json or {}).get("access_setup") or {}
            if setup.get("source") == "facility_onboarding" and setup.get("facility_id") == facility.id:
                user.is_active = True
                activated_accounts += 1
        entry.on(
            facility,
            before=before,
            after={
                "is_active": True,
                "onboarding_status": "ACTIVE",
                "activated_at": now.isoformat(),
                "activated_accounts": activated_accounts,
            },
        )
    return facility
