from __future__ import annotations

import re

from sqlalchemy import select
from starlette.testclient import TestClient

from db.models import AuditLog, Facility, PlatformSetting, UserAccount
from db.session import SessionLocal
from web.main import app


PASSWORD = "Rabta@2026"
SYSTEM = "admin@punjab-teaching.rabta.pk"
PROVINCIAL = "dr.tariq@south-punjab-dhq.rabta.pk"
EMERGENCY = "control.room@south-punjab-dhq.rabta.pk"
PHLEBOTOMIST = "n.bibi@punjab-teaching.rabta.pk"


def sign_in(web: TestClient, email: str) -> None:
    response = web.post("/login", data={"email": email, "password": PASSWORD})
    assert response.status_code == 200


def test_system_administration_is_not_a_clinical_superuser():
    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, SYSTEM)
        dashboard = web.get("/app/dashboard")
        donors = web.get("/app/donors")

    assert dashboard.status_code == 200
    assert "Donor Register" not in dashboard.text.split('aria-label="Main navigation"')[1].split("</nav>")[0]
    assert donors.status_code == 403


def test_scope_selector_changes_provincial_facility_workspace():
    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, PROVINCIAL)
        dashboard = web.get("/app/dashboard")
        assert 'name="scope"' in dashboard.text
        changed = web.post(
            "/app/switch-scope",
            data={"scope": "PROVINCE", "next": "/insights/facilities"},
        )

    assert changed.status_code == 200
    assert "Network situation map" in changed.text
    assert "Facilities in scope" in changed.text


def test_emergency_unit_links_open_redacted_evidence_instead_of_403():
    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, EMERGENCY)
        command = web.get("/insights/command-centre")
        match = re.search(r'href="(/insights/unit-evidence/[^"]+)"', command.text)
        assert match, "the Command Centre should expose at least one planning-evidence unit"
        evidence = web.get(match.group(1))

    assert evidence.status_code == 200
    assert "Network-safe evidence" in evidence.text
    assert "/app/donors/" not in evidence.text
    assert "Patient reference" not in evidence.text


def test_phlebotomist_donor_record_hides_inventory_recall_action():
    db = SessionLocal()
    try:
        user = db.scalar(select(UserAccount).where(UserAccount.email == PHLEBOTOMIST))
        facility = db.get(Facility, user.facility_id)
    finally:
        db.close()

    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, PHLEBOTOMIST)
        register = web.get("/app/donors")
        match = re.search(r'href="(/app/donors/[0-9a-f-]{36})"', register.text)
        assert match, f"expected a donor at {facility.name_en}"
        donor = web.get(match.group(1))

    assert donor.status_code == 200
    assert "/app/inventory/recall/" not in donor.text


def test_admin_weights_are_persisted_and_audited():
    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, SYSTEM)
        admin = web.get("/admin")
        assert admin.status_code == 200
        response = web.post(
            "/admin/optimizer-weights",
            data={
                "shortage": "1100",
                "waste": "210",
                "transport": "1",
                "fixed_dispatch": "25",
                "substitution": "15",
                "capacity": "50",
            },
        )
        assert response.status_code == 200

    db = SessionLocal()
    try:
        setting = db.get(PlatformSetting, "optimizer.weights")
        audit = db.scalar(
            select(AuditLog)
            .where(AuditLog.action == "optimizer.weights.update")
            .order_by(AuditLog.created_at.desc())
        )
    finally:
        db.close()

    assert setting.value_json["shortage"] == 1100
    assert audit is not None


def test_admin_can_see_and_request_decision_refresh(monkeypatch):
    from services import intelligence_refresh

    calls = []
    monkeypatch.setattr(
        intelligence_refresh,
        "run_pending",
        lambda **kwargs: calls.append(kwargs),
    )

    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, SYSTEM)
        admin = web.get("/admin")
        assert admin.status_code == 200
        assert "Decision intelligence" in admin.text
        assert "Refresh decision data now" in admin.text
        response = web.post("/admin/refresh-intelligence")

    assert response.status_code == 200
    assert "Decision intelligence refresh started" in response.text
    assert len(calls) == 1
    assert calls[0]["force"] is True


def test_bench_role_cannot_request_decision_refresh(monkeypatch):
    from services import intelligence_refresh

    calls = []
    monkeypatch.setattr(
        intelligence_refresh,
        "run_pending",
        lambda **kwargs: calls.append(kwargs),
    )

    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, PHLEBOTOMIST)
        response = web.post(
            "/admin/refresh-intelligence", follow_redirects=False
        )

    assert response.status_code == 403
    assert calls == []


def test_facility_settings_round_trip_is_scoped_and_audited():
    db = SessionLocal()
    try:
        facility = db.scalar(select(Facility).order_by(Facility.name_en))
        assert facility is not None
        facility_id = facility.id
        original = {
            "integration_mode": facility.integration_mode,
            "shares_inventory": facility.shares_inventory,
            "shares_contact": facility.shares_contact,
            "network_response_sla_minutes": facility.network_response_sla_minutes,
        }
        changed_sla = 75 if original["network_response_sla_minutes"] != 75 else 90
    finally:
        db.close()

    try:
        with TestClient(app, follow_redirects=True) as web:
            sign_in(web, SYSTEM)
            response = web.post(
                f"/insights/facilities/{facility_id}/settings",
                data={
                    "integration_mode": "API",
                    "network_response_sla_minutes": str(changed_sla),
                    "shares_inventory": "yes",
                    "shares_contact": "yes",
                },
            )

        assert response.status_code == 200
        assert "Facility settings saved" in response.text

        db = SessionLocal()
        try:
            updated = db.get(Facility, facility_id)
            audit = db.scalar(
                select(AuditLog)
                .where(
                    AuditLog.action == "facility.settings.update",
                    AuditLog.entity_id == facility_id,
                )
                .order_by(AuditLog.created_at.desc())
            )
            assert updated.integration_mode == "API"
            assert updated.network_response_sla_minutes == changed_sla
            assert updated.shares_inventory is True
            assert updated.shares_contact is True
            assert audit is not None
        finally:
            db.close()
    finally:
        db = SessionLocal()
        try:
            restored = db.get(Facility, facility_id)
            for key, value in original.items():
                setattr(restored, key, value)
            db.commit()
        finally:
            db.close()


def test_governance_pages_render_in_english_and_urdu_without_raw_tokens():
    db = SessionLocal()
    try:
        facility_id = db.scalar(select(Facility.id).order_by(Facility.name_en))
    finally:
        db.close()

    paths = [
        "/insights/facilities",
        f"/insights/facilities/{facility_id}",
        "/insights/analytics",
        "/admin",
    ]
    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, SYSTEM)
        for language in ("en", "ur"):
            web.post("/app/language", data={"lang": language, "next": "/app/dashboard"})
            for path in paths:
                response = web.get(path)
                assert response.status_code == 200, f"{language} {path}"
                assert "[governance." not in response.text
                assert "[common." not in response.text
                assert "[scope." not in response.text

        export = web.get("/insights/analytics/export.csv")
        assert export.status_code == 200
        assert export.headers["content-type"].startswith("text/csv")
