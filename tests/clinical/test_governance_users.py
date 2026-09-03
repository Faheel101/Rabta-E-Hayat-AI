from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.auth import Role
from db.base import Base
from db.models import AuditLog, Facility, Organization, UserAccount, UserSession
from services.audit import Actor, ServiceError
from services.governance_service import create_user, reset_user_password
from web.security import hash_password, now, verify_password


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'users.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture()
def organization_and_facility(db):
    organization = Organization(
        id="organization-a",
        code="ORG_A",
        name_en="Organization A",
        org_type="HOSPITAL_GROUP",
        province="Punjab",
        is_active=True,
    )
    facility = Facility(
        id="facility-a",
        code="FAC_A",
        organization_id=organization.id,
        name_en="Facility A",
        facility_type="DHQ",
        district="Lahore",
        province="Punjab",
        latitude=31.5,
        longitude=74.3,
        is_active=True,
    )
    db.add(organization)
    db.flush()
    db.add(facility)
    db.commit()
    return organization, facility


@pytest.fixture()
def system_actor():
    return Actor(
        user_id="system-admin",
        display_name="System Administrator",
        role=Role.SYSTEM_ADMIN.value,
        organization_wide=True,
    )


def test_create_user_assigns_explicit_scope_and_forces_password_change(
    db, organization_and_facility, system_actor
):
    organization, facility = organization_and_facility
    user = create_user(
        db,
        system_actor,
        organization_id=organization.id,
        facility_id=facility.id,
        full_name="  New   Blood Bank Officer ",
        email="NEW.OFFICER@EXAMPLE.TEST",
        job_title="Duty officer",
        role=Role.BLOOD_BANK_OFFICER.value,
        temporary_password="Safe@Temporary2026!",
    )

    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.action == "user.access.create",
            AuditLog.entity_id == user.id,
        )
    )
    assert user.full_name == "New Blood Bank Officer"
    assert user.email == "new.officer@example.test"
    assert user.organization_id == organization.id
    assert user.facility_id == facility.id
    assert user.must_change_password is True
    assert verify_password("Safe@Temporary2026!", user.password_hash)
    assert audit is not None


def test_operational_role_cannot_be_created_without_a_facility(
    db, organization_and_facility, system_actor
):
    organization, _facility = organization_and_facility
    with pytest.raises(ServiceError) as error:
        create_user(
            db,
            system_actor,
            organization_id=organization.id,
            facility_id=None,
            full_name="Unscoped Officer",
            email="unscoped@example.test",
            role=Role.BLOOD_BANK_OFFICER.value,
            temporary_password="Safe@Temporary2026!",
        )
    assert error.value.code == "FACILITY_REQUIRED"
    assert db.scalar(
        select(UserAccount).where(UserAccount.email == "unscoped@example.test")
    ) is None


def test_organization_administrator_cannot_create_a_foreign_tenant_user(
    db, organization_and_facility
):
    organization, facility = organization_and_facility
    other = Organization(
        id="organization-b",
        code="ORG_B",
        name_en="Organization B",
        org_type="HOSPITAL_GROUP",
        province="Punjab",
        is_active=True,
    )
    db.add(other)
    db.commit()
    actor = Actor(
        user_id="coordinator-a",
        display_name="Coordinator A",
        role=Role.RBC_COORDINATOR.value,
        organization_id=organization.id,
        facility_id=facility.id,
        scope_facility_ids=(facility.id,),
    )

    with pytest.raises(ServiceError) as error:
        create_user(
            db,
            actor,
            organization_id=other.id,
            facility_id=None,
            full_name="Foreign Coordinator",
            email="foreign@example.test",
            role=Role.RBC_COORDINATOR.value,
            temporary_password="Safe@Temporary2026!",
        )
    assert error.value.code == "ORGANIZATION_NOT_FOUND"


def test_password_reset_revokes_live_sessions_and_is_audited(
    db, organization_and_facility, system_actor
):
    organization, facility = organization_and_facility
    user = UserAccount(
        id="target-user",
        organization_id=organization.id,
        facility_id=facility.id,
        email="target@example.test",
        password_hash=hash_password("Old@Password2026!"),
        full_name="Target User",
        role=Role.BLOOD_BANK_OFFICER.value,
        is_active=True,
        must_change_password=False,
    )
    session = UserSession(
        id="live-session",
        user_id=user.id,
        active_facility_id=facility.id,
        created_at=now(),
        last_seen_at=now(),
        expires_at=now() + timedelta(hours=1),
    )
    db.add(user)
    db.flush()
    db.add(session)
    db.commit()

    reset_user_password(
        db,
        system_actor,
        user.id,
        temporary_password="New@Temporary2027!",
    )

    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.action == "user.access.password_reset",
            AuditLog.entity_id == user.id,
        )
    )
    assert user.must_change_password is True
    assert verify_password("New@Temporary2027!", user.password_hash)
    assert session.revoked_at is not None
    assert audit is not None
    assert audit.after_json["sessions_revoked"] == 1
