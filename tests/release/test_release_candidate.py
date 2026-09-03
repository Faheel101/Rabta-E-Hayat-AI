"""Sprint 13 release contract, live dossier, and evidence rendering."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import sys

from starlette.testclient import TestClient

from core.release import (
    ACCEPTANCE_IDENTITIES,
    OPERATING_MODE_PROOFS,
    WORKFLOW_DOMAINS,
    unique_roles,
    workflow_ids,
)
from db.session import SessionLocal
from scripts.demo_smoke import as_markdown, summary
from scripts.release_candidate import GateResult, _run_command, render_markdown
from services.release_acceptance import acceptance_snapshot
from web.main import app


PASSWORD = "Rabta@2026"
SYSTEM_ADMIN = "admin@punjab-teaching.rabta.pk"
PROVINCIAL_ADMIN = "dr.tariq@south-punjab-dhq.rabta.pk"
REPO = Path(__file__).resolve().parents[2]


def _sign_in(client: TestClient, email: str) -> None:
    response = client.post("/login", data={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    assert "/login" not in str(response.url)


def test_acceptance_contract_covers_seven_roles_and_independent_lab_release():
    assert len(unique_roles()) == 7
    assert len(ACCEPTANCE_IDENTITIES) == 8
    assert {identity.code for identity in ACCEPTANCE_IDENTITIES} >= {"LAB_A", "LAB_B"}
    assert next(item for item in ACCEPTANCE_IDENTITIES if item.code == "LAB_A").email != next(
        item for item in ACCEPTANCE_IDENTITIES if item.code == "LAB_B"
    ).email

    all_ids = [workflow_id for item in WORKFLOW_DOMAINS for workflow_id in item.workflow_ids]
    assert len(all_ids) == len(set(all_ids))
    assert workflow_ids() >= {
        "PH-01",
        "LAB-03",
        "BBO-05",
        "RBC-03",
        "EC-02",
        "PA-03",
        "SA-05",
    }


def test_every_claimed_evidence_module_exists_and_route_contract_is_unambiguous():
    for workflow in WORKFLOW_DOMAINS:
        for evidence in workflow.evidence:
            assert (REPO / evidence).is_file(), evidence

    for identity in ACCEPTANCE_IDENTITIES:
        assert set(identity.allowed_paths).isdisjoint(identity.denied_paths)
        assert identity.start_path in identity.allowed_paths


def test_live_acceptance_snapshot_proves_roles_modes_and_reference_data():
    db = SessionLocal()
    try:
        snapshot = acceptance_snapshot(db)
    finally:
        db.close()

    assert snapshot["version"] == "0.15.0"
    assert snapshot["release_ready"] is True
    assert all(gate["ready"] for gate in snapshot["gates"])
    assert all(identity["ready"] for identity in snapshot["identities"])
    assert all(mode["ready"] for mode in snapshot["modes"])
    assert snapshot["counts"]["facilities"] >= 30
    assert snapshot["counts"]["available_units"] > 0
    assert snapshot["counts"]["two_person_lab_facilities"] > 0


def test_release_workspace_is_system_admin_only_and_explains_the_whole_chain():
    with TestClient(app, follow_redirects=True) as client:
        _sign_in(client, SYSTEM_ADMIN)
        response = client.get("/admin/release")

    assert response.status_code == 200
    assert "One release dossier for every role" in response.text
    assert "End-to-end workflow chain" in response.text
    assert "Operating-model proof" in response.text
    assert "SA-05" in response.text
    assert "7/7" in response.text
    assert "[release." not in response.text
    for identity in ACCEPTANCE_IDENTITIES:
        assert identity.email in response.text

    with TestClient(app, follow_redirects=True) as client:
        _sign_in(client, PROVINCIAL_ADMIN)
        refused = client.get("/admin/release")

    assert refused.status_code == 403


def test_release_workspace_has_native_urdu_and_no_fallback_tokens():
    with TestClient(app, follow_redirects=True) as client:
        _sign_in(client, SYSTEM_ADMIN)
        client.post(
            "/app/language",
            data={"lang": "ur", "next": "/admin/release"},
        )
        response = client.get("/admin/release")

    assert response.status_code == 200
    assert 'dir="rtl"' in response.text
    assert "ریلیز قبولیت" in response.text
    assert "ابتدا سے انتہا عملی سلسلہ" in response.text
    assert "[release." not in response.text


def test_live_journey_evidence_has_machine_and_human_readable_summaries():
    results = [
        {
            "journey": "SERVICE",
            "identity": "Service health",
            "role": "SERVICE",
            "path": "/health/live",
            "status": 200,
            "expected_status": 200,
            "ms": 12.5,
            "budget_ms": 3000.0,
            "passed": True,
        },
        {
            "journey": "SYSTEM",
            "identity": "System Administrator",
            "role": "SYSTEM_ADMIN",
            "path": "/admin/release",
            "status": 200,
            "expected_status": 200,
            "ms": 50.0,
            "budget_ms": 3000.0,
            "passed": True,
        },
    ]
    totals = summary(results)
    markdown = as_markdown(results, "http://127.0.0.1:8765")

    assert totals == {
        "probes": 2,
        "passed": 2,
        "failed": 0,
        "identities": 1,
        "roles": 1,
        "slowest_ms": 50.0,
    }
    assert "2/2 probes" in markdown
    assert "System Administrator" in markdown


def test_release_dossier_preserves_a_blocked_docker_gate_as_not_passed():
    report = {
        "service": "Rabta-e-Hayat",
        "version": "0.15.0",
        "status": "BLOCKED",
        "generated_at": "2026-08-29T12:00:00+00:00",
        "gates": [
            asdict(
                GateResult(
                    "docker",
                    "Docker Compose contract",
                    "BLOCKED",
                    True,
                    "Docker CLI is unavailable.",
                )
            )
        ],
        "database_evidence": {
            "scenario_date": "2026-08-06",
            "counts": {
                "organizations": 10,
                "facilities": 30,
                "donors": 1,
                "donations": 1,
                "available_units": 1,
                "open_requests": 1,
                "transfers": 1,
                "active_alerts": 1,
                "audit_events": 1,
                "two_person_lab_facilities": 1,
            },
            "identities": [],
            "modes": [
                {
                    **asdict(OPERATING_MODE_PROOFS[0]),
                    "ready": True,
                    "detail": "Reference facility",
                }
            ],
        },
    }
    markdown = render_markdown(report)

    assert "**Status:** BLOCKED" in markdown
    assert "Docker Compose contract | **BLOCKED** | Yes" in markdown
    assert "must be completed" in markdown


def test_release_subprocess_can_isolate_demo_only_environment(monkeypatch):
    monkeypatch.setenv("RABTA_SHOW_DEMO_LOGINS", "1")
    result = _run_command(
        "environment",
        "Environment isolation",
        [
            sys.executable,
            "-c",
            "import os; raise SystemExit(os.getenv('RABTA_SHOW_DEMO_LOGINS') != '0')",
        ],
        environment_overrides={"RABTA_SHOW_DEMO_LOGINS": "0"},
    )

    assert result.status == "PASS"
