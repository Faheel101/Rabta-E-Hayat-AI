"""Safety contract for the governed Qwen boundary.

These tests are deliberately provider-free. They inject a fake Qwen response so
the release suite proves privacy, traceability, fallback and non-mutation even
when the demo machine has no internet connection or API key.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import sessionmaker

from db.models import AiInteraction, BloodUnit, Facility, Organization, Transfer
from services import ai_service
from services.audit import Actor, ServiceError


def _actor(db, *, second_tenant: bool = False) -> Actor:
    organizations = list(db.scalars(select(Organization).order_by(Organization.id)).all())
    organization = organizations[1 if second_tenant else 0]
    facility = db.scalar(
        select(Facility)
        .where(Facility.organization_id == organization.id)
        .order_by(Facility.id)
    )
    return Actor(
        user_id=f"ai-test-{organization.id}",
        display_name="AI release test",
        role="SYSTEM_ADMIN",
        facility_id=facility.id,
        organization_id=organization.id,
        scope_facility_ids=(facility.id,),
    )


def _facts(db, actor: Actor, *, signals: int = 3) -> dict:
    facility = db.get(Facility, actor.facility_id)
    return {
        "active_facility_name": facility.name_en,
        "shortage_signals": signals,
        "authority": "Read-only recommendation; human approval is required.",
    }


def _output(facts: dict, *, signals: int | None = None) -> dict:
    count = facts["shortage_signals"] if signals is None else signals
    return {
        "headline": "Source-bound operational review",
        "paragraphs": [
            f"There are {count} shortage signals at {facts['active_facility_name']}."
        ],
        "actions": [
            {
                "label": "Review the evidence",
                "reason": "An authorised person must make any operational change.",
                "priority": "high",
            }
        ],
        "limitations": ["This response cannot approve or move blood."],
    }


def _fallback(facts: dict, _language: str) -> dict:
    return _output(facts)


@pytest.fixture()
def ai_db(scratch_database):
    db = sessionmaker(bind=scratch_database, expire_on_commit=False)()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def configured_fake_boundary(monkeypatch):
    monkeypatch.setattr(ai_service, "AI_ENABLED", True)
    monkeypatch.setattr(ai_service, "QWEN_API_KEY", "test-only-key")
    monkeypatch.setattr(ai_service, "AI_FEATURES", set(ai_service.FEATURE_POLICIES))
    monkeypatch.setattr(ai_service, "AI_DAILY_TOKEN_BUDGET", 10_000_000)
    monkeypatch.setattr(ai_service, "AI_DAILY_BUDGET_USD", 1000.0)
    ai_service.reset_circuit_for_tests()
    yield
    ai_service.reset_circuit_for_tests()


def test_valid_qwen_output_is_verified_without_storing_raw_question(ai_db):
    actor = _actor(ai_db)
    facts = _facts(ai_db, actor, signals=7)
    question = f"What should be reviewed first? {uuid4()}"
    calls = []

    def provider(messages, _policy):
        calls.append(messages)
        return _output(facts), {
            "request_id": "qwen-test-request",
            "prompt_tokens": 120,
            "completion_tokens": 45,
        }

    result = ai_service.generate(
        ai_db,
        actor,
        feature="ask_rabta",
        language="en",
        facts=facts,
        question=question,
        fallback=_fallback,
        provider_call=provider,
    )

    row = ai_db.get(AiInteraction, result.interaction_id)
    assert result.status == "VERIFIED"
    assert result.validation["source_bound"] is True
    assert row.question_hash and row.question_hash != question
    assert question not in json.dumps(row.result_json)
    assert question not in json.dumps(row.validation_json)
    assert row.prompt_tokens == 120 and row.completion_tokens == 45
    assert "question" not in {column["name"] for column in inspect(ai_db.bind).get_columns("ai_interaction")}
    assert len(calls) == 1


def test_qwen_call_uses_the_governed_openai_compatible_contract(monkeypatch):
    captured = {}
    response_content = {
        "headline": "Connection available",
        "paragraphs": ["The governed read-only connection is available."],
        "actions": [],
        "limitations": ["No operational action is permitted."],
    }

    class Response:
        status_code = 200
        headers = {"x-request-id": "header-request"}

        @staticmethod
        def json():
            return {
                "id": "qwen-response-id",
                "choices": [{"message": {"content": json.dumps(response_content)}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            }

    class Client:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, endpoint, *, headers, json):
            captured.update(endpoint=endpoint, headers=headers, payload=json)
            return Response()

    monkeypatch.setattr(ai_service.httpx, "Client", Client)
    monkeypatch.setattr(ai_service, "QWEN_BASE_URL", "https://dashscope.example/v1")
    monkeypatch.setattr(ai_service, "QWEN_MODEL", "qwen3.7-plus")
    monkeypatch.setattr(ai_service, "QWEN_API_KEY", "test-only-key")

    output, meta = ai_service._call_qwen(
        [{"role": "system", "content": "read only"}],
        ai_service.FEATURE_POLICIES["ask_rabta"],
    )

    assert output == response_content
    assert captured["endpoint"] == "https://dashscope.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-only-key"
    assert captured["payload"]["model"] == "qwen3.7-plus"
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["enable_thinking"] is False
    assert captured["payload"]["temperature"] == 0.1
    assert captured["payload"]["stream"] is False
    assert captured["timeout"] == ai_service.AI_TIMEOUT_SECONDS
    assert meta == {
        "request_id": "qwen-response-id",
        "prompt_tokens": 11,
        "completion_tokens": 7,
    }


def test_sensitive_fact_fails_closed_before_any_provider_call(ai_db):
    actor = _actor(ai_db)
    facts = {**_facts(ai_db, actor), "donor_name": "A person", "phone": "03001234567"}
    called = False

    def provider(_messages, _policy):
        nonlocal called
        called = True
        return _output(facts), {}

    result = ai_service.generate(
        ai_db,
        actor,
        feature="ask_rabta",
        language="en",
        facts=facts,
        fallback=_fallback,
        provider_call=provider,
        force=True,
    )

    assert result.status == "FALLBACK"
    assert result.fallback_reason == "sensitive_fields_redacted"
    assert called is False
    assert set(result.validation["redacted_fields"]) == {"donor_name", "phone"}


@pytest.mark.parametrize(
    "question",
    (
        "What is donor id 123?",
        "Contact me at person@example.com",
        "Check patient 35202-1234567-1",
        "Call 03001234567 about stock",
        "Tell Dr Ahmed Raza about this shortage",
        "My API key is dashscope-test-secret-123456789",
        "مریض کا نام احمد رضا ہے",
    ),
)
def test_identity_bearing_questions_are_blocked_before_persistence(ai_db, question):
    actor = _actor(ai_db)
    before = ai_db.scalar(select(func.count()).select_from(AiInteraction))

    with pytest.raises(ServiceError, match="identifiers"):
        ai_service.generate(
            ai_db,
            actor,
            feature="ask_rabta",
            language="en",
            facts=_facts(ai_db, actor),
            question=question,
            fallback=_fallback,
            provider_call=lambda *_: (_output(_facts(ai_db, actor)), {}),
        )

    after = ai_db.scalar(select(func.count()).select_from(AiInteraction))
    assert after == before


def test_invented_number_retries_once_then_uses_checked_fallback(ai_db):
    actor = _actor(ai_db)
    facts = _facts(ai_db, actor, signals=4)
    attempts = 0

    def provider(_messages, _policy):
        nonlocal attempts
        attempts += 1
        return _output(facts, signals=999), {}

    result = ai_service.generate(
        ai_db,
        actor,
        feature="command_brief",
        language="en",
        facts=facts,
        fallback=_fallback,
        provider_call=provider,
        force=True,
    )

    assert attempts == ai_service.AI_MAX_RETRIES + 1
    assert result.status == "FALLBACK"
    assert result.fallback_reason == "output_validation_failed"
    assert "999" not in json.dumps(result.content)
    assert any("untraceable numerals" in issue for issue in result.validation["provider_issues"])


def test_provider_failure_never_breaks_the_user_workflow(ai_db):
    actor = _actor(ai_db)
    facts = _facts(ai_db, actor, signals=5)
    attempts = 0

    def unavailable(_messages, _policy):
        nonlocal attempts
        attempts += 1
        raise ai_service.ProviderError("timeout", "provider timed out")

    result = ai_service.generate(
        ai_db,
        actor,
        feature="ask_rabta",
        language="en",
        facts=facts,
        fallback=_fallback,
        provider_call=unavailable,
        force=True,
    )

    assert attempts == ai_service.AI_MAX_RETRIES + 1
    assert result.status == "FALLBACK"
    assert result.fallback_reason == "timeout"
    assert result.validation["source_bound"] is True


def test_verified_cache_is_scoped_to_the_tenant(ai_db):
    first_actor = _actor(ai_db)
    second_actor = _actor(ai_db, second_tenant=True)
    calls = 0

    def provider(messages, _policy):
        nonlocal calls
        calls += 1
        payload = json.loads(messages[1]["content"])
        facts = payload["VALIDATED_FACTS"]
        return _output(facts), {}

    unique_signals = int(str(uuid4().int)[:5])
    first_facts = _facts(ai_db, first_actor, signals=unique_signals)
    first = ai_service.generate(
        ai_db,
        first_actor,
        feature="command_brief",
        language="en",
        facts=first_facts,
        fallback=_fallback,
        provider_call=provider,
    )
    cached = ai_service.generate(
        ai_db,
        first_actor,
        feature="command_brief",
        language="en",
        facts=first_facts,
        fallback=_fallback,
        provider_call=provider,
    )
    second_facts = _facts(ai_db, second_actor, signals=unique_signals)
    second = ai_service.generate(
        ai_db,
        second_actor,
        feature="command_brief",
        language="en",
        facts=second_facts,
        fallback=_fallback,
        provider_call=provider,
    )

    assert first.status == second.status == "VERIFIED"
    assert cached.cache_hit is True and cached.interaction_id != first.interaction_id
    assert ai_db.get(AiInteraction, cached.interaction_id).cache_hit is True
    assert second.interaction_id != first.interaction_id
    assert calls == 2


def test_ai_generation_cannot_mutate_inventory_or_transfer_state(ai_db):
    actor = _actor(ai_db)
    before = (
        ai_db.scalar(select(func.count()).select_from(BloodUnit)),
        ai_db.scalar(select(func.count()).select_from(Transfer)),
        tuple(ai_db.execute(select(Transfer.status, func.count()).group_by(Transfer.status)).all()),
    )
    facts = _facts(ai_db, actor, signals=6)

    result = ai_service.generate(
        ai_db,
        actor,
        feature="optimizer_advisor",
        language="en",
        facts=facts,
        fallback=_fallback,
        provider_call=lambda *_: (_output(facts), {}),
        force=True,
    )
    after = (
        ai_db.scalar(select(func.count()).select_from(BloodUnit)),
        ai_db.scalar(select(func.count()).select_from(Transfer)),
        tuple(ai_db.execute(select(Transfer.status, func.count()).group_by(Transfer.status)).all()),
    )

    assert result.status == "VERIFIED"
    assert after == before
