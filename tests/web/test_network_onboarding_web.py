from __future__ import annotations

import uuid

from sqlalchemy import select
from starlette.testclient import TestClient

from db.models import Facility, UserAccount
from db.session import SessionLocal
from web.main import app


PASSWORD = "Rabta@2026"
SYSTEM = "admin@punjab-teaching.rabta.pk"
PROVINCIAL = "dr.tariq@south-punjab-dhq.rabta.pk"


def sign_in(client: TestClient, email: str, password: str = PASSWORD):
    return client.post("/login", data={"email": email, "password": password})


def payload(token: str) -> dict:
    return {
        "organization_action": "NEW",
        "operating_mode": "HOSPITAL_NETWORK",
        "organization_code": f"WEB_{token}",
        "organization_name_en": f"Web Hospital {token}",
        "organization_name_ur": "ویب ہسپتال",
        "province": "Punjab",
        "contact_email": f"ops-{token.lower()}@example.test",
        "contact_phone": "03001234567",
        "facility_code": f"WBB_{token}",
        "facility_name_en": f"Web Blood Bank {token}",
        "facility_name_ur": "ویب بلڈ بینک",
        "facility_type": "DHQ",
        "district": "Lahore",
        "division": "Lahore",
        "latitude": "31.5204",
        "longitude": "74.3587",
        "bed_count": "250",
        "integration_mode": "CSV",
        "shares_inventory": "yes",
        "shares_contact": "yes",
        "network_response_sla_minutes": "60",
        "has_trauma_centre": "yes",
        "has_obgyn": "yes",
        "fridge_capacity": "250",
        "freezer_capacity": "120",
        "platelet_capacity": "48",
        "admin_name": "Web First Officer",
        "admin_email": f"first-{token.lower()}@example.test",
        "admin_role": "BLOOD_BANK_OFFICER",
        "temporary_password": "Web@SecureStart26!",
    }


def test_system_admin_can_create_review_and_activate_a_complete_draft(monkeypatch):
    from services import intelligence_refresh

    refreshes = []
    monkeypatch.setattr(
        intelligence_refresh,
        "run_pending",
        lambda **kwargs: refreshes.append(kwargs),
    )
    token = uuid.uuid4().hex[:7].upper()
    with TestClient(app, follow_redirects=False) as client:
        assert sign_in(client, SYSTEM).status_code == 303
        assert client.get("/admin/onboarding").status_code == 200
        new_page = client.get("/admin/onboarding/new")
        assert new_page.status_code == 200
        assert "Create safe draft" in new_page.text

        created = client.post("/admin/onboarding", data=payload(token))
        assert created.status_code == 303
        assert created.headers["location"].startswith("/admin/onboarding/")
        draft_url = created.headers["location"]
        review = client.get(draft_url)
        assert review.status_code == 200
        assert "7/7" in review.text
        assert "Ready to activate" in review.text

        facility_id = draft_url.rsplit("/", 1)[-1]
        activated = client.post(f"{draft_url}/activate")
        assert activated.status_code == 303
        assert activated.headers["location"] == f"/insights/facilities/{facility_id}"

    with SessionLocal() as db:
        facility = db.get(Facility, facility_id)
        user = db.scalar(select(UserAccount).where(UserAccount.facility_id == facility_id))
        assert facility.is_active is True
        assert facility.onboarding_status == "ACTIVE"
        assert user.is_active is True
        assert user.must_change_password is True
        new_email = user.email
    assert len(refreshes) == 1

    # A temporary credential can reach only the password gate, then becomes a
    # normal facility-scoped account after the user replaces it.
    with TestClient(app, follow_redirects=False) as first_login:
        login = sign_in(first_login, new_email, "Web@SecureStart26!")
        assert login.headers["location"] == "/account/password"
        assert first_login.get("/app/dashboard").headers["location"] == "/account/password"
        changed = first_login.post(
            "/account/password",
            data={
                "current_password": "Web@SecureStart26!",
                "new_password": "Web@PersonalSecure27!",
                "confirm_password": "Web@PersonalSecure27!",
            },
        )
        assert changed.headers["location"] == "/app/dashboard"
        assert first_login.get("/app/dashboard").status_code == 200


def test_non_system_administrator_cannot_open_network_onboarding():
    with TestClient(app, follow_redirects=False) as client:
        sign_in(client, PROVINCIAL)
        assert client.get("/admin/onboarding").status_code == 403
        assert client.post("/admin/onboarding", data=payload("DENIED1")).status_code == 403


def test_onboarding_pages_have_equivalent_urdu_without_raw_tokens():
    with TestClient(app, follow_redirects=True) as client:
        sign_in(client, SYSTEM)
        client.post(
            "/app/language",
            data={"lang": "ur", "next": "/admin/onboarding/new"},
        )
        page = client.get("/admin/onboarding/new")

    assert page.status_code == 200
    assert 'dir="rtl"' in page.text
    assert "ادارے اور مرکز کی شمولیت" in page.text
    assert "[governance." not in page.text
    assert "[auth." not in page.text
    assert "[common." not in page.text
