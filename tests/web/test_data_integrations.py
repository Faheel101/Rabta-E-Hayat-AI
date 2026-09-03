"""Data workspace and clean API sub-application integration tests."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from db.models import Component, Facility, StorageLocation
from services import integration_service
from services.audit import Actor
from web.api import api_app
from web.deps import get_db
from web.main import app

PASSWORD = "Rabta@2026"
COORDINATOR = "s.fatima@punjab-teaching.rabta.pk"
OFFICER = "dr.ahmed@punjab-teaching.rabta.pk"


def sign_in(client: TestClient, email: str):
    response = client.post("/login", data={"email": email, "password": PASSWORD})
    assert response.status_code == 200


def test_data_workspace_is_role_gated_bilingual_and_not_a_placeholder():
    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, COORDINATOR)
        english = web.get("/data")
        web.post("/app/language", data={"lang": "ur", "next": "/data"})
        urdu = web.get("/data")
    assert english.status_code == urdu.status_code == 200
    assert "Bring every blood bank into one trusted data contract" in english.text
    assert "Manual CSV onboarding" in english.text
    assert "FHIR R4" in english.text and "HL7 v2" in english.text
    assert 'dir="rtl"' in urdu.text
    assert "ہر بلڈ بینک" in urdu.text
    assert "[data." not in english.text + urdu.text

    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, OFFICER)
        forbidden = web.get("/data")
    assert forbidden.status_code == 403


def test_csv_templates_and_openapi_are_available():
    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, COORDINATOR)
        inventory = web.get("/data/templates/inventory.csv")
        demand = web.get("/data/templates/demand.csv")
        docs = web.get("/api/openapi.json")
    assert inventory.status_code == demand.status_code == docs.status_code == 200
    assert "source_system_ref,din,component_code" in inventory.text
    assert "units_requested,units_issued" in demand.text
    schema = docs.json()
    assert schema["info"]["title"] == "Rabta-e-Hayat Integration API"
    assert "/v1/imports/fhir/preview" in schema["paths"]


def test_versioned_api_requires_key_and_enforces_scope(scratch_database):
    factory = sessionmaker(bind=scratch_database, expire_on_commit=False)
    db = factory()
    component = db.scalar(select(Component).where(Component.code == "PRBC"))
    facility = db.scalar(
        select(Facility)
        .join(StorageLocation, StorageLocation.facility_id == Facility.id)
        .where(
            Facility.organization_id.is_not(None),
            StorageLocation.is_active.is_(True),
            StorageLocation.is_quarantine.is_(False),
            StorageLocation.target_temp_min_c <= component.storage_temp_min_c,
            StorageLocation.target_temp_max_c >= component.storage_temp_max_c,
        )
    )
    actor = Actor(
        user_id="test:api-admin",
        display_name="API Administrator",
        role="SYSTEM_ADMIN",
        facility_id=facility.id,
        organization_id=facility.organization_id,
        organization_wide=True,
    )
    _client, secret = integration_service.create_api_client(
        db,
        actor,
        organization_id=facility.organization_id,
        name="API route test",
        scopes=["facilities:read", "imports:write"],
        facility_ids=[facility.id],
    )
    db.close()

    def override_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    api_app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(api_app) as api:
            assert api.get("/v1/health").status_code == 200
            assert api.get("/v1/facilities").status_code == 401
            facilities = api.get(
                "/v1/facilities", headers={"X-Rabta-Key": secret}
            )
            assert facilities.status_code == 200
            assert facilities.json()["data"][0]["id"] == facility.id
            assert api.get(
                "/v1/inventory",
                params={"facility_id": facility.id},
                headers={"X-Rabta-Key": secret},
            ).status_code == 403

            token = uuid4().hex[:10]
            preview = api.post(
                "/v1/imports/canonical/preview",
                headers={"X-Rabta-Key": secret},
                json={
                    "facility_id": facility.id,
                    "data_type": "INVENTORY",
                    "source_name": "api.json",
                    "records": [
                        {
                            "source_system_ref": f"API-{token}",
                            "din": f"A{token}",
                            "component_code": "PRBC",
                            "blood_group": "O+",
                            "collected_at": "2026-08-15T08:00:00+00:00",
                            "expires_at": "2026-09-15T08:00:00+00:00",
                            "status": "AVAILABLE",
                            "screening_status": "PASSED",
                            "volume_ml": 300,
                            "is_leucodepleted": True,
                            "is_irradiated": False,
                        }
                    ],
                },
            )
            assert preview.status_code == 201, preview.text
            assert preview.json()["status"] == "READY"
            batch_id = preview.json()["id"]
            committed = api.post(
                f"/v1/imports/{batch_id}/commit",
                headers={"X-Rabta-Key": secret},
                json={"confirm": True},
            )
            assert committed.status_code == 200, committed.text
            assert committed.json()["status"] == "COMMITTED"
    finally:
        api_app.dependency_overrides.clear()
