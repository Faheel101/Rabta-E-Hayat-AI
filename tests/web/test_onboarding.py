"""Sprint 9: role-aware onboarding, persistence, and zero-training navigation."""

from __future__ import annotations

from copy import deepcopy

from sqlalchemy import select
from starlette.testclient import TestClient

from app.auth import Role
from db.models import AuditLog, UserAccount
from db.session import SessionLocal
from web.guidance import build_role_guide, greeting_name
from web.main import app
from web.navigation import build_nav
from web.routers.facility import ENABLED_NAV


PASSWORD = "Rabta@2026"
COORDINATOR = "s.fatima@punjab-teaching.rabta.pk"
PHLEBOTOMIST = "n.bibi@punjab-teaching.rabta.pk"
LAB_TECH = "r.aslam@punjab-teaching.rabta.pk"


def make_client() -> TestClient:
    return TestClient(app, follow_redirects=True)


def sign_in(client: TestClient, email: str):
    return client.post("/login", data={"email": email, "password": PASSWORD})


def set_preferences(email: str, value: dict | None) -> None:
    with SessionLocal() as db:
        user = db.scalar(select(UserAccount).where(UserAccount.email == email))
        user.preferences_json = deepcopy(value)
        db.commit()


def get_preferences(email: str) -> dict | None:
    with SessionLocal() as db:
        user = db.scalar(select(UserAccount).where(UserAccount.email == email))
        return deepcopy(user.preferences_json)


def test_help_workspace_is_authenticated_and_role_specific():
    with make_client() as client:
        response = client.get("/app/getting-started")
        assert "/login" in str(response.url)

        sign_in(client, COORDINATOR)
        response = client.get("/app/getting-started")

    assert response.status_code == 200
    assert "Help &amp; Workflows" in response.text or "Help & Workflows" in response.text
    assert "Turn network-wide shortage and expiry signals" in response.text
    assert "Read the Command Centre" in response.text
    assert "Review governed transfers" in response.text
    assert "Check data and integrations" in response.text


def test_bench_roles_receive_only_work_they_can_open():
    with make_client() as client:
        sign_in(client, PHLEBOTOMIST)
        body = client.get("/app/getting-started").text
        navigation = body.split('aria-label="Main navigation"')[1].split("</nav>")[0]

        assert "Find or register the donor" in body
        assert "Complete donor screening" in body
        assert "Record the collection" in body
        assert "Read the Command Centre" not in body
        assert "Guided Demonstration" not in body
        assert "Help &amp; Workflows" in body or "Help & Workflows" in body
        assert "Inventory" not in navigation
        assert "Clinical Requests" not in navigation
        assert 'id="facility-switch"' not in body

        assert client.get("/app/donors").status_code == 200
        assert client.get("/app/sessions").status_code == 200

    with make_client() as client:
        sign_in(client, LAB_TECH)
        body = client.get("/app/getting-started").text

    assert "Clear the laboratory queue" in body
    assert "Prepare viable components" in body
    assert "Verify available inventory" in body
    assert "Record the collection" not in body


def test_facility_pinned_user_cannot_switch_to_a_sibling_facility():
    with SessionLocal() as db:
        user = db.scalar(select(UserAccount).where(UserAccount.email == PHLEBOTOMIST))
        from db.models import Facility

        sibling = db.scalar(
            select(Facility.id).where(
                Facility.organization_id == user.organization_id,
                Facility.id != user.facility_id,
            )
        )

    with make_client() as client:
        sign_in(client, PHLEBOTOMIST)
        response = client.post(
            "/app/switch-facility",
            data={"facility_id": sibling},
            follow_redirects=False,
        )

    assert response.status_code == 404


def test_first_run_welcome_is_persisted_per_account_and_can_be_restarted():
    original = get_preferences(COORDINATOR)

    try:
        set_preferences(COORDINATOR, {})

        with make_client() as client:
            sign_in(client, COORDINATOR)
            first = client.get("/app/dashboard")
            assert "New to Rabta-e-Hayat?" in first.text

            completed = client.post(
                "/app/getting-started/complete",
                data={"next": "/app/dashboard"},
            )
            assert "New to Rabta-e-Hayat?" not in completed.text
            assert "Orientation complete" in completed.text

        saved = get_preferences(COORDINATOR)
        assert saved["onboarding"]["version"] == 1
        assert saved["onboarding"]["completed_at"]

        with make_client() as client:
            sign_in(client, COORDINATOR)
            assert "New to Rabta-e-Hayat?" not in client.get("/app/dashboard").text
            restarted = client.post("/app/getting-started/restart")
            assert "New to Rabta-e-Hayat?" in restarted.text

        with SessionLocal() as db:
            user_id = db.scalar(
                select(UserAccount.id).where(UserAccount.email == COORDINATOR)
            )
            actions = set(
                db.scalars(
                    select(AuditLog.action).where(
                        AuditLog.entity_type == "user_account",
                        AuditLog.entity_id == user_id,
                    )
                ).all()
            )

        assert {"onboarding.completed", "onboarding.restarted"} <= actions
    finally:
        set_preferences(COORDINATOR, original)


def test_onboarding_redirect_target_cannot_leave_the_application():
    original = get_preferences(COORDINATOR)

    try:
        set_preferences(COORDINATOR, {})

        with make_client() as client:
            sign_in(client, COORDINATOR)
            response = client.post(
                "/app/getting-started/complete",
                data={"next": "//evil.example/phish"},
                follow_redirects=False,
            )

        assert response.headers["location"] == "/app/dashboard"
    finally:
        set_preferences(COORDINATOR, original)


def test_help_workspace_has_equivalent_urdu_and_no_translation_tokens():
    with make_client() as client:
        sign_in(client, COORDINATOR)
        client.post(
            "/app/language",
            data={"lang": "ur", "next": "/app/getting-started"},
        )
        response = client.get("/app/getting-started")

    assert 'dir="rtl"' in response.text
    assert "مدد اور طریقۂ کار" in response.text
    assert "آپ کا کردار، تین مراحل میں" in response.text
    assert "[onboarding." not in response.text


def test_every_role_guide_has_three_concrete_in_app_actions():
    for role in Role:
        guide = build_role_guide(role.value, {}, language="en")

        assert len(guide["tasks"]) == 3
        assert all(task["url"].startswith("/") for task in guide["tasks"])
        assert all(task["title"] and task["body"] for task in guide["tasks"])


def test_navigation_starts_with_three_role_focused_destinations_without_duplicates():
    for role in Role:
        sections = build_nav(
            role=role.value,
            current_path="/app/dashboard",
            enabled_keys=ENABLED_NAV,
            language="en",
        )

        assert sections[0]["focus"] is True
        assert sections[0]["label"] == "My work"
        assert len(sections[0]["entries"]) == 3
        assert sections[0]["entries"][0]["key"] == "dashboard"

        urls = [item["url"] for section in sections for item in section["entries"]]
        assert len(urls) == len(set(urls)), f"{role.value} navigation repeats a destination"


def test_secondary_navigation_and_dashboard_evidence_use_progressive_disclosure():
    with make_client() as client:
        sign_in(client, COORDINATOR)
        response = client.get("/app/dashboard")

    assert 'class="nav-disclosure"' in response.text
    assert 'class="disclosure-card mt-4"' in response.text
    assert "Inventory detail" in response.text


def test_dashboard_tables_are_contained_inside_responsive_grid_items():
    template = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "web"
        / "templates"
        / "app"
        / "dashboard.html"
    ).read_text(encoding="utf-8")

    assert 'grid min-w-0 gap-4' in template
    assert template.count('class="card min-w-0"') >= 2


def test_greeting_keeps_professional_title_without_double_punctuation():
    assert greeting_name("Dr. Ahmed Raza") == "Dr. Ahmed"
    assert greeting_name("System Administrator") == "System"


def test_donor_register_explains_where_new_registration_starts():
    with make_client() as client:
        sign_in(client, PHLEBOTOMIST)
        response = client.get("/app/donors")

    assert response.status_code == 200
    assert "Registering someone new?" in response.text
    assert (
        "Register and screen" in response.text
        or "Open collection workspace" in response.text
    )
