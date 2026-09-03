"""Sprint 8 guided demonstration and explicit synthetic-clock boundaries."""

from __future__ import annotations

from sqlalchemy import func, select
from starlette.testclient import TestClient

from db.models import Donor, UserAccount
from db.session import SessionLocal
from web.main import app

PASSWORD = "Rabta@2026"
OFFICER = "dr.ahmed@punjab-teaching.rabta.pk"
PHLEBOTOMIST = "n.bibi@punjab-teaching.rabta.pk"


def sign_in(web: TestClient, email: str = OFFICER) -> None:
    response = web.post("/login", data={"email": email, "password": PASSWORD})
    assert response.status_code == 200


def test_showcase_requires_authentication():
    with TestClient(app, follow_redirects=True) as web:
        response = web.get("/showcase")

    assert response.status_code == 200
    assert "/login" in str(response.url)


def test_showcase_is_live_integrated_story_not_static_pitch_copy():
    db = SessionLocal()

    try:
        user = db.scalar(select(UserAccount).where(UserAccount.email == OFFICER))
        donor_count = int(
            db.scalar(
                select(func.count())
                .select_from(Donor)
                .where(Donor.organization_id == user.organization_id)
            )
            or 0
        )
    finally:
        db.close()

    with TestClient(app, follow_redirects=True) as web:
        sign_in(web)
        response = web.get("/showcase")

    assert response.status_code == 200
    assert "One blood network. Every decision connected." in response.text
    assert "The integrated four-minute story" in response.text
    assert "Live proof points" in response.text
    assert f"{donor_count:,}" in response.text
    assert "/insights/command-centre" in response.text
    assert "/insights/transfer-plan" in response.text
    assert "/insights/simulator" in response.text
    assert "[showcase." not in response.text


def test_fixed_synthetic_clock_is_explicit_on_every_authenticated_page():
    with TestClient(app, follow_redirects=True) as web:
        sign_in(web)
        dashboard = web.get("/app/dashboard")
        showcase = web.get("/showcase")

    for response in (dashboard, showcase):
        assert "Synthetic demonstration" in response.text
        assert "Fixed, reproducible scenario as of 06 Aug 2026, 08:00" in response.text
        assert "no live hospital data" in response.text


def test_showcase_and_synthetic_clock_have_native_urdu_copy():
    with TestClient(app, follow_redirects=True) as web:
        sign_in(web)
        web.post("/app/language", data={"lang": "ur", "next": "/showcase"})
        response = web.get("/showcase")

    assert response.status_code == 200
    assert 'dir="rtl"' in response.text
    assert "خون کا ایک نیٹ ورک۔ ہر فیصلہ باہم مربوط۔" in response.text
    assert "مصنوعی مظاہرہ" in response.text
    assert "کوئی حقیقی ہسپتال ڈیٹا نہیں" in response.text
    assert "[showcase." not in response.text


def test_bench_role_cannot_open_or_see_the_showcase():
    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, PHLEBOTOMIST)
        response = web.get("/showcase")
        dashboard = web.get("/app/dashboard")

    assert response.status_code == 403
    assert "/showcase" not in dashboard.text


def test_health_contract_discloses_synthetic_scenario_date():
    with TestClient(app) as web:
        live = web.get("/health/live").json()
        ready = web.get("/health/ready").json()
        api = web.get("/api/v1/health").json()

    for payload in (live, ready, api):
        assert payload["data_mode"] == "synthetic"
        assert payload["scenario_date"] == "2026-08-06"
