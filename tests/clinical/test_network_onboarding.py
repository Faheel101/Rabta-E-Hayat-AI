from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app.auth import Role
from db.base import Base
from db.models import (
    AuditLog,
    BloodGroup,
    Facility,
    IntegrationFeed,
    IntelligenceRefreshState,
    Organization,
    StorageLocation,
    UserAccount,
)
from services.audit import Actor, PermissionDenied, ServiceError
from services.network_onboarding import activate, create_draft, readiness


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'onboarding.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        for code, abo, rh, share in [
            ("B+", "B", "+", 33.0),
            ("O+", "O", "+", 30.0),
            ("A+", "A", "+", 22.0),
            ("AB+", "AB", "+", 8.0),
            ("O-", "O", "-", 2.5),
            ("B-", "B", "-", 2.0),
            ("A-", "A", "-", 1.7),
            ("AB-", "AB", "-", 0.8),
        ]:
            session.add(BloodGroup(code=code, abo=abo, rh=rh, population_pct_pk=share))
        session.commit()
        yield session


@pytest.fixture()
def system_actor():
    return Actor(
        user_id="system-admin",
        display_name="System Administrator",
        role=Role.SYSTEM_ADMIN.value,
        organization_wide=True,
    )


def values(token: str | None = None) -> dict:
    token = (token or uuid.uuid4().hex[:8]).upper()
    return {
        "organization_action": "NEW",
        "operating_mode": "STANDALONE",
        "existing_organization_id": None,
        "organization_code": f"ORG_{token}",
        "organization_name_en": f"Safety Hospital {token}",
        "organization_name_ur": "محفوظ ہسپتال",
        "province": "Punjab",
        "contact_email": f"ops-{token.lower()}@example.test",
        "contact_phone": "03001234567",
        "facility_code": f"BB_{token}",
        "facility_name_en": f"Safety Blood Bank {token}",
        "facility_name_ur": "محفوظ بلڈ بینک",
        "facility_type": "DHQ",
        "district": "Lahore",
        "division": "Lahore",
        "latitude": 31.5204,
        "longitude": 74.3587,
        "bed_count": 250,
        "parent_rbc_id": None,
        "integration_mode": "CSV",
        "shares_inventory": True,
        "shares_contact": True,
        "network_response_sla_minutes": 60,
        "has_trauma_centre": True,
        "has_obgyn": True,
        "has_oncology": False,
        "has_thalassaemia_centre": False,
        "has_cardiac_surgery": False,
        "fridge_capacity": 250,
        "freezer_capacity": 120,
        "platelet_capacity": 48,
        "admin_name": "First Officer",
        "admin_email": f"officer-{token.lower()}@example.test",
        "admin_role": Role.BLOOD_BANK_OFFICER.value,
        "temporary_password": "Safe@Start2026!",
    }


def test_draft_is_complete_but_invisible_and_does_not_dirty_intelligence(db, system_actor):
    facility = create_draft(db, system_actor, **values("DRAFT01"))
    organization = db.get(Organization, facility.organization_id)
    user = db.scalar(select(UserAccount).where(UserAccount.facility_id == facility.id))
    feed = db.scalar(select(IntegrationFeed).where(IntegrationFeed.facility_id == facility.id))
    stores = list(
        db.scalars(select(StorageLocation).where(StorageLocation.facility_id == facility.id))
    )

    assert facility.is_active is False
    assert facility.onboarding_status == "DRAFT"
    assert organization.is_active is False
    assert user.is_active is False
    assert user.must_change_password is True
    assert feed.status == "NEVER_SYNCED"
    assert len(stores) == 4
    assert any(row.is_quarantine for row in stores)
    assert set(facility.min_reserve_policy_json) == {
        "WB", "PRBC", "PLT_RD", "PLT_APH", "FFP", "CRYO"
    }
    assert all(
        len(groups) == 8 for groups in facility.min_reserve_policy_json.values()
    )
    # Standalone is a privacy posture, not merely a label.
    assert facility.shares_inventory is False
    assert facility.shares_contact is False
    assert db.get(IntelligenceRefreshState, "decision-intelligence") is None
    assert readiness(db, facility)["ready"] is True


def test_activation_rechecks_readiness_enables_access_and_invalidates_marts(db, system_actor):
    facility = create_draft(db, system_actor, **values("ACTIVE1"))
    activated = activate(db, system_actor, facility.id)
    user = db.scalar(select(UserAccount).where(UserAccount.facility_id == facility.id))
    organization = db.get(Organization, facility.organization_id)
    state = db.get(IntelligenceRefreshState, "decision-intelligence")
    actions = set(
        db.scalars(
            select(AuditLog.action).where(AuditLog.entity_id == facility.id)
        ).all()
    )

    assert activated.is_active is True
    assert activated.onboarding_status == "ACTIVE"
    assert activated.activated_by == system_actor.display_name
    assert organization.is_active is True
    assert user.is_active is True
    assert state.status == "DIRTY"
    assert state.reason == "facility.onboarding.activate"
    assert {
        "network_onboarding.draft.create",
        "facility.onboarding.activate",
    } <= actions


def test_readiness_blocks_activation_when_quarantine_is_removed(db, system_actor):
    facility = create_draft(db, system_actor, **values("BLOCK01"))
    for store in db.scalars(
        select(StorageLocation).where(
            StorageLocation.facility_id == facility.id,
            StorageLocation.is_quarantine.is_(True),
        )
    ):
        store.is_active = False
    db.commit()

    assert readiness(db, facility)["ready"] is False
    with pytest.raises(ServiceError, match="quarantine"):
        activate(db, system_actor, facility.id)
    assert facility.is_active is False


def test_readiness_blocks_activation_without_a_group_level_reserve_policy(
    db, system_actor
):
    facility = create_draft(db, system_actor, **values("RESERVE1"))
    facility.min_reserve_policy_json = {}
    db.commit()

    state = readiness(db, facility)
    assert state["ready"] is False
    assert next(
        check for check in state["checks"] if check["key"] == "reserve_policy"
    )["passed"] is False
    with pytest.raises(ServiceError, match="reserve_policy"):
        activate(db, system_actor, facility.id)


def test_non_system_role_cannot_create_a_tenant(db):
    actor = Actor(
        user_id="coordinator",
        display_name="Coordinator",
        role=Role.RBC_COORDINATOR.value,
        organization_wide=True,
    )
    with pytest.raises(PermissionDenied):
        create_draft(db, actor, **values("DENIED1"))
    assert db.scalar(select(func.count()).select_from(Organization)) == 0


def test_invalid_rbc_relationship_rolls_back_the_entire_draft(db, system_actor):
    payload = values("ROLLBACK")
    payload.update(
        operating_mode="RBC_NETWORK",
        facility_type="DHQ",
        parent_rbc_id=None,
    )
    with pytest.raises(ServiceError) as error:
        create_draft(db, system_actor, **payload)
    assert error.value.code == "PARENT_RBC_REQUIRED"
    assert db.scalar(select(func.count()).select_from(Organization)) == 0
    assert db.scalar(select(func.count()).select_from(Facility)) == 0
    assert db.scalar(select(func.count()).select_from(UserAccount)) == 0


def test_existing_organization_is_reused_without_changing_its_governance(db, system_actor):
    organization = Organization(
        id=str(uuid.uuid4()),
        code="EXISTING",
        name_en="Existing Network",
        org_type="HOSPITAL_GROUP",
        province="Punjab",
        network_opt_in=True,
        settings_json={"operating_mode": "HOSPITAL_NETWORK"},
        is_active=True,
    )
    db.add(organization)
    db.commit()
    payload = values("EXIST01")
    payload.update(
        organization_action="EXISTING",
        existing_organization_id=organization.id,
        operating_mode="STANDALONE",  # Existing governance remains authoritative.
    )

    facility = create_draft(db, system_actor, **payload)

    assert facility.organization_id == organization.id
    assert facility.shares_inventory is True
    assert db.scalar(select(func.count()).select_from(Organization)) == 1


def test_weak_temporary_password_creates_nothing(db, system_actor):
    payload = values("WEAKPWD")
    payload["temporary_password"] = "short"
    with pytest.raises(ServiceError) as error:
        create_draft(db, system_actor, **payload)
    assert error.value.code == "PASSWORD_WEAK"
    assert db.scalar(select(func.count()).select_from(Organization)) == 0
