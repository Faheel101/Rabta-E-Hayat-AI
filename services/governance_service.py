"""Audited administration of facilities, users and optimizer policy."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Permission, Role
from db.models import Facility, Organization, PlatformSetting, UserAccount, UserSession, new_id
from engines.optimizer.plan import weights as configured_weights
from services.audit import Actor, ServiceError, audited, require, snapshot
from services.network_onboarding import validate_temporary_password
from web.security import hash_password


FACILITY_FIELDS = (
    "integration_mode",
    "shares_inventory",
    "shares_contact",
    "network_response_sla_minutes",
)
USER_FIELDS = ("role", "is_active", "facility_id")
_UNSET = object()
FACILITY_PINNED_ROLES = {
    Role.PHLEBOTOMIST.value,
    Role.LAB_TECHNOLOGIST.value,
    Role.BLOOD_BANK_OFFICER.value,
}


def _manageable_organization(db: Session, actor: Actor, organization_id: str) -> Organization:
    require(actor, Permission.MANAGE_USERS, "manage users")
    organization = db.get(Organization, organization_id)
    if organization is None or not organization.is_active:
        raise ServiceError("ORGANIZATION_NOT_FOUND", "Organization not found in this administrative scope.")
    if actor.role != Role.SYSTEM_ADMIN.value and actor.organization_id != organization.id:
        raise ServiceError("ORGANIZATION_NOT_FOUND", "Organization not found in this administrative scope.")
    return organization


def _role(actor: Actor, value: str) -> Role:
    try:
        selected = Role(value)
    except ValueError as exc:
        raise ServiceError("ROLE_INVALID", "Choose a supported role.") from exc
    if selected is Role.SYSTEM_ADMIN and actor.role != Role.SYSTEM_ADMIN.value:
        raise ServiceError("ROLE_ESCALATION", "Only a system administrator can assign that role.")
    return selected


def _facility_assignment(
    db: Session,
    organization_id: str,
    facility_id: str | None,
    role: Role,
) -> str | None:
    selected_id = str(facility_id or "").strip() or None
    if selected_id:
        facility = db.scalar(
            select(Facility).where(
                Facility.id == selected_id,
                Facility.organization_id == organization_id,
                Facility.is_active.is_(True),
            )
        )
        if facility is None:
            raise ServiceError("FACILITY_NOT_FOUND", "Choose an active facility in this organization.")
    if role.value in FACILITY_PINNED_ROLES and selected_id is None:
        raise ServiceError("FACILITY_REQUIRED", "This operational role must be assigned to one facility.")
    return selected_id


def create_user(
    db: Session,
    actor: Actor,
    *,
    organization_id: str,
    facility_id: str | None,
    full_name: str,
    email: str,
    role: str,
    temporary_password: str,
    job_title: str | None = None,
) -> UserAccount:
    organization = _manageable_organization(db, actor, organization_id)
    name = " ".join(str(full_name or "").strip().split())[:160]
    normalized_email = str(email or "").strip().lower()[:255]
    if len(name) < 2:
        raise ServiceError("NAME_REQUIRED", "Enter the account holder's full name.", field="full_name")
    if "@" not in normalized_email or "." not in normalized_email.rsplit("@", 1)[-1]:
        raise ServiceError("EMAIL_INVALID", "Enter a valid sign-in email.", field="email")
    if db.scalar(select(UserAccount.id).where(func.lower(UserAccount.email) == normalized_email)):
        raise ServiceError("USER_EMAIL_EXISTS", "That sign-in email is already in use.", field="email")
    selected_role = _role(actor, role)
    assigned_facility = _facility_assignment(
        db, organization.id, facility_id, selected_role
    )
    password = validate_temporary_password(temporary_password)
    user = UserAccount(
        id=new_id(),
        organization_id=organization.id,
        facility_id=assigned_facility,
        email=normalized_email,
        password_hash=hash_password(password),
        full_name=name,
        job_title=" ".join(str(job_title or "").strip().split())[:120] or None,
        role=selected_role.value,
        preferences_json={},
        is_active=True,
        must_change_password=True,
    )
    with audited(db, actor, "user.access.create", "user_account", user.id) as entry:
        db.add(user)
        entry.on(
            user,
            after={
                "organization_id": organization.id,
                "facility_id": assigned_facility,
                "email": normalized_email,
                "role": selected_role.value,
                "is_active": True,
                "must_change_password": True,
            },
        )
    return user


def update_facility_settings(
    db: Session,
    actor: Actor,
    facility_id: str,
    *,
    integration_mode: str,
    shares_inventory: bool,
    shares_contact: bool,
    network_response_sla_minutes: int,
) -> Facility:
    require(actor, Permission.EDIT_FACILITY_SETTINGS, "edit facility settings")
    facility = db.get(Facility, facility_id)
    if facility is None or facility.id not in set(actor.scope_facility_ids):
        raise ServiceError("FACILITY_NOT_FOUND", "Facility not found in this scope.")
    mode = (integration_mode or "").strip().upper()
    if mode not in {"SIMULATED", "CSV", "FHIR", "HL7", "API"}:
        raise ServiceError("INTEGRATION_MODE_INVALID", "Choose a supported integration mode.")
    sla = int(network_response_sla_minutes)
    if not 5 <= sla <= 1440:
        raise ServiceError("SLA_INVALID", "Response SLA must be between 5 and 1,440 minutes.")

    before = snapshot(facility, FACILITY_FIELDS)
    with audited(db, actor, "facility.settings.update", "facility", facility.id) as entry:
        facility.integration_mode = mode
        facility.shares_inventory = bool(shares_inventory)
        facility.shares_contact = bool(shares_contact)
        facility.network_response_sla_minutes = sla
        entry.on(facility, before=before, after=snapshot(facility, FACILITY_FIELDS))
    return facility


def optimizer_weights(db: Session) -> dict:
    setting = db.get(PlatformSetting, "optimizer.weights")
    return configured_weights(dict(setting.value_json or {}) if setting else None)


def update_optimizer_weights(
    db: Session,
    actor: Actor,
    values: dict[str, float],
) -> PlatformSetting:
    require(actor, Permission.CHANGE_OPTIMIZER_WEIGHTS, "change optimizer weights")
    expected = {
        "shortage",
        "waste",
        "transport",
        "fixed_dispatch",
        "substitution",
        "capacity",
    }
    if set(values) != expected:
        raise ServiceError("WEIGHTS_INCOMPLETE", "Every optimizer weight is required.")
    cleaned = {key: float(value) for key, value in values.items()}
    if any(value < 0 or value > 100_000 for value in cleaned.values()):
        raise ServiceError("WEIGHT_INVALID", "Weights must be between 0 and 100,000.")
    if cleaned["shortage"] <= cleaned["transport"]:
        raise ServiceError(
            "WEIGHT_SAFETY_INVALID",
            "Shortage prevention must remain more important than transport cost.",
        )

    setting = db.get(PlatformSetting, "optimizer.weights")
    before = dict(setting.value_json or {}) if setting else None
    if setting is None:
        setting = PlatformSetting(
            key="optimizer.weights",
            value_json=cleaned,
            updated_by=actor.display_name,
            updated_at=datetime.now(timezone.utc),
        )
        db.add(setting)
    with audited(
        db,
        actor,
        "optimizer.weights.update",
        "platform_setting",
        setting.key,
    ) as entry:
        setting.value_json = cleaned
        setting.updated_by = actor.display_name
        setting.updated_at = datetime.now(timezone.utc)
        entry.on(entity_id=setting.key, before=before, after=cleaned)
    return setting


def update_user(
    db: Session,
    actor: Actor,
    user_id: str,
    *,
    role: str,
    is_active: bool,
    facility_id: str | None | object = _UNSET,
) -> UserAccount:
    require(actor, Permission.MANAGE_USERS, "manage users")
    target = db.get(UserAccount, user_id)
    if target is None or (
        actor.role != Role.SYSTEM_ADMIN.value
        and target.organization_id != actor.organization_id
    ):
        raise ServiceError("USER_NOT_FOUND", "User not found in this administrative scope.")
    if target.id == actor.user_id and not is_active:
        raise ServiceError("SELF_DEACTIVATION", "You cannot deactivate your own account.")
    selected_role = _role(actor, role)
    if target.id == actor.user_id and selected_role.value != target.role:
        raise ServiceError("SELF_ROLE_CHANGE", "You cannot change your own administrative role.")
    if target.role == Role.SYSTEM_ADMIN.value and (
        selected_role is not Role.SYSTEM_ADMIN or not is_active
    ):
        active_admins = int(
            db.scalar(
                select(func.count()).select_from(UserAccount).where(
                    UserAccount.role == Role.SYSTEM_ADMIN.value,
                    UserAccount.is_active.is_(True),
                )
            )
            or 0
        )
        if active_admins <= 1:
            raise ServiceError("LAST_SYSTEM_ADMIN", "The final active system administrator cannot be removed.")

    assigned_facility = target.facility_id
    if facility_id is not _UNSET:
        assigned_facility = _facility_assignment(
            db,
            target.organization_id,
            facility_id if isinstance(facility_id, str) else None,
            selected_role,
        )
    elif selected_role.value in FACILITY_PINNED_ROLES and not assigned_facility:
        raise ServiceError("FACILITY_REQUIRED", "This operational role must be assigned to one facility.")

    before = snapshot(target, USER_FIELDS)
    with audited(db, actor, "user.access.update", "user_account", target.id) as entry:
        target.role = selected_role.value
        target.is_active = bool(is_active)
        target.facility_id = assigned_facility
        entry.on(target, before=before, after=snapshot(target, USER_FIELDS))
    return target


def reset_user_password(
    db: Session,
    actor: Actor,
    user_id: str,
    *,
    temporary_password: str,
) -> UserAccount:
    require(actor, Permission.MANAGE_USERS, "reset user access")
    target = db.get(UserAccount, user_id)
    if target is None or (
        actor.role != Role.SYSTEM_ADMIN.value
        and target.organization_id != actor.organization_id
    ):
        raise ServiceError("USER_NOT_FOUND", "User not found in this administrative scope.")
    password = validate_temporary_password(temporary_password)
    sessions = list(
        db.scalars(
            select(UserSession).where(
                UserSession.user_id == target.id,
                UserSession.revoked_at.is_(None),
            )
        ).all()
    )
    stamp = datetime.now(timezone.utc)
    was_forced = bool(target.must_change_password)
    with audited(db, actor, "user.access.password_reset", "user_account", target.id) as entry:
        target.password_hash = hash_password(password)
        target.must_change_password = True
        target.failed_login_count = 0
        target.locked_until = None
        for session in sessions:
            session.revoked_at = stamp
        entry.on(
            target,
            before={"must_change_password": was_forced, "sessions_revoked": 0},
            after={"must_change_password": True, "sessions_revoked": len(sessions)},
        )
    return target
