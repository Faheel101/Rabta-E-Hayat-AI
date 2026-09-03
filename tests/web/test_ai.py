"""End-user and administrator contracts for Rabta AI."""

from __future__ import annotations

from sqlalchemy import select
from starlette.testclient import TestClient

from db.models import AiInteraction
from db.session import SessionLocal
from web.main import app


PASSWORD = "Rabta@2026"
SYSTEM = "admin@punjab-teaching.rabta.pk"
OFFICER = "dr.ahmed@punjab-teaching.rabta.pk"
PHLEBOTOMIST = "n.bibi@punjab-teaching.rabta.pk"


def sign_in(web: TestClient, email: str = OFFICER) -> None:
    response = web.post("/login", data={"email": email, "password": PASSWORD})
    assert response.status_code == 200


def test_ai_workspace_is_clear_offline_safe_and_available_to_every_role():
    for email in (OFFICER, PHLEBOTOMIST):
        with TestClient(app, follow_redirects=True) as web:
            sign_in(web, email)
            page = web.get("/ai")

        assert page.status_code == 200
        assert "A governed copilot for blood-supply decisions" in page.text
        assert "Offline-safe mode ready" in page.text
        assert "Human approval always required" in page.text
        assert "Ask Rabta AI" in page.text
        assert "[ai." not in page.text


def test_offline_question_returns_a_labelled_source_bound_answer():
    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, OFFICER)
        response = web.post(
            "/ai/ask",
            data={"question": "What should my current team review first?"},
        )

    assert response.status_code == 200
    assert "Offline safe mode" in response.text
    assert "Bound to visible source data" in response.text
    assert "An authorized user must make every operational decision" in response.text

    db = SessionLocal()
    try:
        row = db.scalar(
            select(AiInteraction)
            .where(AiInteraction.feature == "ask_rabta")
            .order_by(AiInteraction.created_at.desc())
        )
    finally:
        db.close()
    assert row is not None and row.status == "FALLBACK"
    assert row.question_hash
    assert not hasattr(row, "question") and not hasattr(row, "prompt")


def test_identity_question_is_rejected_without_echoing_it_as_an_answer():
    private_value = "person@example.com"
    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, OFFICER)
        response = web.post(
            "/ai/ask",
            data={"question": f"Tell me about donor {private_value}"},
        )

    assert response.status_code == 422
    assert "not allowed to process" in response.text
    assert "Verified AI" not in response.text


def test_command_brief_is_integrated_but_does_not_change_operational_state():
    db = SessionLocal()
    try:
        statuses_before = tuple(
            db.execute(select(AiInteraction.status).order_by(AiInteraction.id)).all()
        )
    finally:
        db.close()

    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, OFFICER)
        response = web.post(
            "/ai/command-brief",
            data={"facility_id": "", "next": "/insights/command-centre"},
        )

    assert response.status_code == 200
    assert "Offline safe mode" in response.text
    assert "Generate with Qwen" in response.text
    assert "does not approve a transfer" in response.text

    db = SessionLocal()
    try:
        statuses_after = tuple(
            db.execute(select(AiInteraction.status).order_by(AiInteraction.id)).all()
        )
    finally:
        db.close()
    assert len(statuses_after) == len(statuses_before) + 1


def test_forecast_guardian_explains_quality_without_retraining_any_model():
    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, OFFICER)
        response = web.post(
            "/ai/forecast-guardian",
            data={"facility_id": "", "next": "/ai"},
        )

    assert response.status_code == 200
    assert "Verified forecast-quality review" in response.text
    assert "This review cannot retrain a forecast or change inventory" in response.text
    assert "Offline safe mode" in response.text


def test_ai_governance_is_admin_only_and_has_no_raw_prompt_surface():
    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, SYSTEM)
        admin = web.get("/admin/ai")

    assert admin.status_code == 200
    assert "Qwen is contained behind one auditable decision boundary" in admin.text
    assert "No raw prompts or questions are stored" in admin.text
    assert "Privacy-minimised AI audit" in admin.text
    assert "QWEN_API_KEY" not in admin.text

    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, SYSTEM)
        advice = web.post("/admin/ai/optimizer-advice")
    assert advice.status_code == 200
    assert "Verified optimizer-policy advisory" in advice.text
    assert "AI cannot save weights or run the optimizer" in advice.text

    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, PHLEBOTOMIST)
        blocked = web.get("/admin/ai")
    assert blocked.status_code == 403


def test_ai_workspace_is_native_rtl_and_has_no_missing_translation_tokens():
    with TestClient(app, follow_redirects=True) as web:
        sign_in(web, OFFICER)
        web.post("/app/language", data={"lang": "ur", "next": "/ai"})
        page = web.get("/ai")
        answer = web.post(
            "/ai/ask",
            data={"question": "موجودہ عملی صورتحال میں پہلے کیا دیکھنا چاہیے؟"},
        )

    assert page.status_code == answer.status_code == 200
    assert 'dir="rtl"' in page.text
    assert "خون کی رسد کے فیصلوں" in page.text
    assert "محفوظ آف لائن موڈ" in answer.text
    assert "[ai." not in page.text and "[ai." not in answer.text
