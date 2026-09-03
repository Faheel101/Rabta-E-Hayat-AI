"""Authenticated web journey for a clinical request and one issued unit."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from core.clock import DEMO_DATETIME
from db.base import Base
from db.models import (
    BloodGroup,
    BloodRequest,
    BloodUnit,
    Compatibility,
    Component,
    Facility,
    Organization,
    UnitIssue,
    UserAccount,
    UserSession,
)
from web.deps import Principal, get_db, require_principal
from web.main import app


@pytest.fixture
def web_context():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    db = maker()

    organization = Organization(
        id="org-web",
        code="ORG-WEB",
        name_en="Web Test Hospital",
        org_type="HOSPITAL_GROUP",
    )
    facility = Facility(
        id="facility-web",
        code="FAC-WEB",
        organization_id=organization.id,
        name_en="Web Test Blood Bank",
        district="Lahore",
    )
    user = UserAccount(
        id="user-web",
        organization_id=organization.id,
        facility_id=facility.id,
        email="web-officer@example.test",
        password_hash="unused",
        full_name="Web Test Officer",
        role="BLOOD_BANK_OFFICER",
    )
    user_session = UserSession(
        id="session-web",
        user_id=user.id,
        active_facility_id=facility.id,
        created_at=DEMO_DATETIME,
        last_seen_at=DEMO_DATETIME,
        expires_at=DEMO_DATETIME + timedelta(hours=1),
    )
    group = BloodGroup(id=1, code="A+", abo="A", rh="+")
    o_negative = BloodGroup(id=2, code="O-", abo="O", rh="-")
    component = Component(
        id=1,
        code="PRBC",
        name_en="Packed red blood cells",
        shelf_life_days=42,
        storage_temp_min_c=2,
        storage_temp_max_c=6,
    )
    unit = BloodUnit(
        id="unit-web",
        din="ZAA2600000001",
        facility_id=facility.id,
        component_id=component.id,
        blood_group_id=group.id,
        volume_ml=350,
        collected_at=DEMO_DATETIME - timedelta(days=3),
        expires_at=DEMO_DATETIME + timedelta(days=20),
        status="AVAILABLE",
        screening_status="PASSED",
    )
    emergency_unit = BloodUnit(
        id="unit-web-o-negative",
        din="ZAA2600000002",
        facility_id=facility.id,
        component_id=component.id,
        blood_group_id=o_negative.id,
        volume_ml=350,
        collected_at=DEMO_DATETIME - timedelta(days=3),
        expires_at=DEMO_DATETIME + timedelta(days=18),
        status="AVAILABLE",
        screening_status="PASSED",
    )
    db.add_all(
        [
            organization,
            facility,
            user,
            user_session,
            group,
            o_negative,
            component,
            unit,
            emergency_unit,
            Compatibility(
                component_id=component.id,
                recipient_group_id=group.id,
                donor_group_id=group.id,
                is_compatible=True,
                preference_rank=1,
            ),
        ]
    )
    db.commit()

    principal = Principal(
        user=user,
        organization=organization,
        session=user_session,
        active_facility=facility,
        org_facilities=[facility],
    )

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[require_principal] = lambda: principal

    try:
        with TestClient(app, follow_redirects=False) as client:
            yield client, db, unit
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def test_request_pages_render_in_english_and_urdu(web_context):
    client, _, _ = web_context

    queue = client.get("/app/requests")
    assert queue.status_code == 200
    assert "Clinical Requests" in queue.text
    assert "New request" in queue.text

    client.post("/app/language", data={"lang": "ur", "next_url": "/app/requests"})
    urdu = client.get("/app/requests")
    assert urdu.status_code == 200
    assert "طبی درخواستیں" in urdu.text
    assert 'dir="rtl"' in urdu.text


def test_complete_request_crossmatch_issue_transfusion_web_journey(web_context):
    client, db, unit = web_context

    created = client.post(
        "/app/requests/new",
        data={
            "patient_ref": "EP-WEB-0001",
            "patient_age_years": "29",
            "patient_sex": "FEMALE",
            "patient_blood_group_id": "1",
            "component_id": "1",
            "units_requested": "1",
            "urgency": "URGENT",
            "clinical_context": "OBSTETRIC",
            "ward": "Labour ward",
            "requested_by": "Dr Web",
            "consultant": "Dr Consultant",
            "required_by": "2026-08-06T10:00",
            "replacement_units_required": "0",
            "notes": "Prepare before theatre transfer.",
        },
    )
    assert created.status_code == 303

    record = db.scalar(select(BloodRequest).where(BloodRequest.patient_ref == "EP-WEB-0001"))
    assert record is not None
    assert created.headers["location"] == f"/app/requests/{record.id}"

    detail = client.get(created.headers["location"])
    assert detail.status_code == 200
    assert unit.din in detail.text
    assert "Compatible stock" in detail.text
    assert "Action timeline" in detail.text
    assert "Clinical request created" in detail.text

    crossmatched = client.post(
        f"/app/requests/{record.id}/crossmatch",
        data={
            "unit_id": unit.id,
            "result": "COMPATIBLE",
            "method": "GEL_CARD",
            "notes": "Compatible by gel card.",
            "override_reason": "",
        },
    )
    assert crossmatched.status_code == 303
    db.refresh(record)
    db.refresh(unit)
    assert record.status == "CROSSMATCHED"
    assert unit.status == "CROSSMATCHED"

    issued = client.post(
        f"/app/requests/{record.id}/issue/{unit.id}",
        data={
            "collected_by": "Nurse Web",
            "patient_ref_confirmation": record.patient_ref,
            "destination_ward": "Labour ward",
        },
    )
    assert issued.status_code == 303
    db.refresh(record)
    db.refresh(unit)
    assert record.status == "ISSUED"
    assert unit.status == "ISSUED"

    issue = db.scalar(select(UnitIssue))
    outcome = client.post(
        f"/app/requests/{record.id}/transfusion/{issue.id}",
        data={
            "outcome": "COMPLETED",
            "reaction_type": "NONE",
            "reaction_severity": "",
            "reaction_notes": "",
        },
    )
    assert outcome.status_code == 303
    db.refresh(record)
    db.refresh(unit)
    assert record.status == "CLOSED"
    assert unit.status == "TRANSFUSED"


def test_request_edit_and_replacement_routes(web_context):
    client, db, _ = web_context
    created = client.post(
        "/app/requests/new",
        data={
            "patient_ref": "EP-WEB-EDIT-0001",
            "patient_blood_group_id": "1",
            "component_id": "1",
            "units_requested": "2",
            "urgency": "ROUTINE",
            "clinical_context": "SURGERY",
            "replacement_units_required": "2",
        },
    )
    record = db.scalar(
        select(BloodRequest).where(BloodRequest.patient_ref == "EP-WEB-EDIT-0001")
    )

    edit_page = client.get(f"/app/requests/{record.id}/edit")
    assert edit_page.status_code == 200
    assert "Edit Clinical Request" in edit_page.text

    updated = client.post(
        f"/app/requests/{record.id}/edit",
        data={
            "patient_ref": record.patient_ref,
            "patient_blood_group_id": "1",
            "component_id": "1",
            "units_requested": "2",
            "urgency": "URGENT",
            "clinical_context": "SURGERY",
            "ward": "Theatre 2",
            "replacement_units_required": "2",
        },
    )
    assert created.status_code == 303
    assert updated.status_code == 303
    db.refresh(record)
    assert record.urgency == "URGENT"
    assert record.ward == "Theatre 2"

    receipt = client.post(
        f"/app/requests/{record.id}/replacement/receipt",
        data={"units_received": "1", "source_reference": "SYN-WEB-DON-001"},
    )
    waiver = client.post(
        f"/app/requests/{record.id}/replacement/waive",
        data={"reason": "Welfare authorization for remaining replacement balance."},
    )
    assert receipt.status_code == 303
    assert waiver.status_code == 303
    db.refresh(record)
    assert record.replacement_units_received == 1
    assert record.replacement_waived is True


def test_emergency_unknown_group_release_web_route(web_context):
    client, db, _ = web_context
    created = client.post(
        "/app/requests/new",
        data={
            "patient_ref": "EP-WEB-EMERGENCY-0001",
            "patient_blood_group_id": "",
            "component_id": "1",
            "units_requested": "1",
            "urgency": "EMERGENCY",
            "clinical_context": "TRAUMA",
            "ward": "Resuscitation bay",
            "replacement_units_required": "0",
        },
    )
    record = db.scalar(
        select(BloodRequest).where(
            BloodRequest.patient_ref == "EP-WEB-EMERGENCY-0001"
        )
    )
    detail = client.get(created.headers["location"])
    assert detail.status_code == 200
    assert "Emergency release" in detail.text
    assert "ZAA2600000002" in detail.text

    issued = client.post(
        f"/app/requests/{record.id}/emergency-issue/unit-web-o-negative",
        data={
            "collected_by": "Nurse Emergency",
            "patient_ref_confirmation": record.patient_ref,
            "destination_ward": "Resuscitation bay",
            "emergency_reason": "Life-threatening haemorrhage before crossmatch completion.",
            "authorized_by": "Dr Emergency",
            "acknowledge_uncrossmatched": "yes",
        },
    )
    assert issued.status_code == 303
    issue = db.scalar(
        select(UnitIssue).where(UnitIssue.request_id == record.id)
    )
    assert issue.release_mode == "EMERGENCY_UNCROSSMATCHED"
    db.refresh(record)
    assert record.status == "ISSUED"


def test_not_returned_custody_route(web_context):
    client, db, unit = web_context
    created = client.post(
        "/app/requests/new",
        data={
            "patient_ref": "EP-WEB-CUSTODY-0001",
            "patient_blood_group_id": "1",
            "component_id": "1",
            "units_requested": "1",
            "urgency": "URGENT",
            "clinical_context": "SURGERY",
            "ward": "Theatre 4",
            "replacement_units_required": "0",
        },
    )
    record = db.scalar(
        select(BloodRequest).where(
            BloodRequest.patient_ref == "EP-WEB-CUSTODY-0001"
        )
    )
    client.post(
        f"/app/requests/{record.id}/crossmatch",
        data={
            "unit_id": unit.id,
            "result": "COMPATIBLE",
            "method": "GEL_CARD",
        },
    )
    client.post(
        f"/app/requests/{record.id}/issue/{unit.id}",
        data={
            "collected_by": "Nurse Custody",
            "patient_ref_confirmation": record.patient_ref,
            "destination_ward": "Theatre 4",
        },
    )
    issue = db.scalar(select(UnitIssue).where(UnitIssue.request_id == record.id))

    response = client.post(
        f"/app/requests/{record.id}/not-returned/{issue.id}",
        data={
            "incident_reference": "SYN-WEB-INCIDENT-001",
            "reason": "Ward investigation could not locate the issued unit.",
        },
    )
    assert created.status_code == 303
    assert response.status_code == 303
    db.refresh(issue)
    db.refresh(unit)
    assert issue.disposition == "NOT_RETURNED"
    assert unit.status == "DISCARDED"
