"""Sprint 10 UAT: visible work, server boundaries, and role-first dashboards."""

from __future__ import annotations

from pathlib import Path
import re

from starlette.testclient import TestClient

from app.auth import Role
from web.guidance import build_role_guide
from web.main import app


PASSWORD = "Rabta@2026"
ACCOUNTS = {
    Role.PHLEBOTOMIST: "n.bibi@punjab-teaching.rabta.pk",
    Role.LAB_TECHNOLOGIST: "r.aslam@punjab-teaching.rabta.pk",
    Role.BLOOD_BANK_OFFICER: "dr.ahmed@punjab-teaching.rabta.pk",
    Role.RBC_COORDINATOR: "s.fatima@punjab-teaching.rabta.pk",
    Role.PROVINCIAL_ADMIN: "dr.tariq@south-punjab-dhq.rabta.pk",
    Role.EMERGENCY_CONTROLLER: "control.room@south-punjab-dhq.rabta.pk",
    Role.SYSTEM_ADMIN: "admin@punjab-teaching.rabta.pk",
}

OPERATIONAL_PAGES = {
    "/app/donors",
    "/app/sessions",
    "/app/lab",
    "/app/processing",
    "/app/inventory",
    "/app/requests",
    "/app/signoff",
}

ALLOWED_OPERATIONAL = {
    Role.PHLEBOTOMIST: {"/app/donors", "/app/sessions"},
    Role.LAB_TECHNOLOGIST: {
        "/app/lab",
        "/app/processing",
        "/app/inventory",
    },
    Role.BLOOD_BANK_OFFICER: OPERATIONAL_PAGES,
    Role.RBC_COORDINATOR: OPERATIONAL_PAGES,
    Role.PROVINCIAL_ADMIN: OPERATIONAL_PAGES,
    Role.EMERGENCY_CONTROLLER: set(),
    Role.SYSTEM_ADMIN: set(),
}


def signed_in(role: Role) -> TestClient:
    client = TestClient(app, follow_redirects=True)
    response = client.post(
        "/login",
        data={"email": ACCOUNTS[role], "password": PASSWORD},
    )
    assert response.status_code == 200
    assert "/login" not in str(response.url)
    return client


def test_every_operational_workspace_has_a_server_side_role_boundary():
    for role, allowed in ALLOWED_OPERATIONAL.items():
        with signed_in(role) as client:
            for path in sorted(OPERATIONAL_PAGES):
                response = client.get(path)
                expected = 200 if path in allowed else 403
                assert response.status_code == expected, (
                    f"{role.value} received {response.status_code} for {path}; "
                    f"expected {expected}"
                )


def test_every_role_guidance_action_is_a_real_authorized_page():
    for role in Role:
        guide = build_role_guide(role.value, {}, language="en")

        with signed_in(role) as client:
            for task in guide["tasks"]:
                response = client.get(task["url"])
                assert response.status_code == 200, (
                    f"{role.value} guidance sends the user to "
                    f"{task['url']} ({response.status_code})"
                )


def test_every_role_navigation_link_resolves_without_a_permission_dead_end():
    for role in Role:
        with signed_in(role) as client:
            dashboard = client.get("/app/dashboard")
            navigation = dashboard.text.split(
                'aria-label="Main navigation"', 1
            )[1].split("</nav>", 1)[0]
            urls = set(re.findall(r'href="(/[^"#]+)', navigation))

            assert urls, f"{role.value} received an empty navigation"

            for url in sorted(urls):
                response = client.get(url)
                assert response.status_code == 200, (
                    f"{role.value} can see {url}, but it returns "
                    f"{response.status_code}"
                )


def test_dashboard_hides_unit_inventory_from_non_inventory_roles():
    for role in (Role.PHLEBOTOMIST, Role.EMERGENCY_CONTROLLER):
        with signed_in(role) as client:
            body = client.get("/app/dashboard").text

        assert 'href="/app/inventory"' not in body
        assert 'href="/app/inventory?' not in body
        assert build_role_guide(role.value, {}, language="en")["handoff"] in body

    with signed_in(Role.LAB_TECHNOLOGIST) as client:
        lab_body = client.get("/app/dashboard").text

    assert 'href="/app/inventory"' in lab_body


def test_dashboard_stock_drilldown_is_keyboard_accessible():
    template = (
        Path(__file__).resolve().parents[2]
        / "web"
        / "templates"
        / "app"
        / "dashboard.html"
    ).read_text(encoding="utf-8")

    assert "onclick=\"window.location=" not in template
    assert 'href="/app/inventory?component={{ row[0] }}"' in template


def test_permission_recovery_page_is_bilingual_and_not_raw_json():
    with signed_in(Role.PHLEBOTOMIST) as client:
        english = client.get("/app/inventory")
        assert english.status_code == 403
        assert "Not permitted" in english.text
        assert 'href="/app/dashboard"' in english.text
        assert '"detail"' not in english.text

        navigation = english.text.split(
            'aria-label="Main navigation"', 1
        )[1].split("</nav>", 1)[0]
        urls = set(re.findall(r'href="(/[^"#]+)', navigation))
        assert urls
        for url in sorted(urls):
            assert client.get(url).status_code == 200, (
                f"permission recovery navigation contains dead link {url}"
            )

        client.post(
            "/app/language",
            data={"lang": "ur", "next": "/app/dashboard"},
        )
        urdu = client.get("/app/inventory")

    assert urdu.status_code == 403
    assert 'dir="rtl"' in urdu.text
    assert "اجازت نہیں" in urdu.text
