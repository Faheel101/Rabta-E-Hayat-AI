"""Sprint 7 release controls: fail closed, recover cleanly, stay observable."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml
from starlette.testclient import TestClient

from config.settings import runtime_config_issues
from engines.forecast.models import LIGHTGBM_AVAILABLE
from scripts.backup_restore import backup_sqlite, restore_sqlite, verify_backup
from scripts.release_check import run_checks
from web.main import app

REPO = Path(__file__).resolve().parents[2]


def test_production_configuration_fails_closed():
    issues = runtime_config_issues(
        {
            "APP_ENV": "production",
            "SECRET_KEY": "dev-only-change-me",
            "SESSION_COOKIE_SECURE": "false",
            "TRUSTED_HOSTS": "*",
            "RABTA_SHOW_DEMO_LOGINS": "1",
        }
    )

    assert any("SECRET_KEY" in issue for issue in issues)
    assert any("SESSION_COOKIE_SECURE" in issue for issue in issues)
    assert any("TRUSTED_HOSTS" in issue for issue in issues)
    assert any("RABTA_SHOW_DEMO_LOGINS" in issue for issue in issues)


def test_a_hardened_production_configuration_is_accepted():
    assert not runtime_config_issues(
        {
            "APP_ENV": "production",
            "SECRET_KEY": "8d77c4a14b5e49e092df44714e4a62d7",
            "SESSION_COOKIE_SECURE": "true",
            "SESSION_COOKIE_SAMESITE": "strict",
            "TRUSTED_HOSTS": "blood.example.pk",
            "RABTA_SHOW_DEMO_LOGINS": "0",
        }
    )


def test_configured_qwen_endpoint_must_be_https_in_production():
    issues = runtime_config_issues(
        {
            "APP_ENV": "production",
            "SECRET_KEY": "8d77c4a14b5e49e092df44714e4a62d7",
            "SESSION_COOKIE_SECURE": "true",
            "SESSION_COOKIE_SAMESITE": "strict",
            "TRUSTED_HOSTS": "blood.example.pk",
            "RABTA_SHOW_DEMO_LOGINS": "0",
            "AI_ENABLED": "true",
            "QWEN_API_KEY": "configured-outside-source-control",
            "QWEN_BASE_URL": "http://unsafe.example/v1",
        }
    )

    assert any("QWEN_BASE_URL" in issue for issue in issues)


def test_health_probes_and_security_headers_are_release_ready():
    with TestClient(app) as client:
        live = client.get("/health/live", headers={"x-request-id": "release-check-123"})
        ready = client.get("/health/ready")

    assert live.status_code == 200
    assert ready.status_code == 200
    assert ready.json()["checks"]["schema"] == "ok"
    assert live.headers["x-request-id"] == "release-check-123"
    assert live.headers["x-content-type-options"] == "nosniff"
    assert live.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in live.headers["content-security-policy"]
    assert "camera=()" in live.headers["permissions-policy"]


def test_runtime_assets_are_versioned_to_prevent_stale_release_css():
    with TestClient(app, follow_redirects=True) as client:
        client.post(
            "/login",
            data={
                "email": "dr.ahmed@punjab-teaching.rabta.pk",
                "password": "Rabta@2026",
            },
        )
        page = client.get("/showcase")

    assert "/static/css/app.css?v=0.15.0" in page.text
    assert "/static/vendor/alpine.min.js?v=0.15.0" in page.text


def test_cross_site_browser_writes_are_rejected():
    with TestClient(app, follow_redirects=False) as client:
        blocked = client.post(
            "/login",
            data={"email": "nobody@example.test", "password": "irrelevant"},
            headers={
                "origin": "https://attacker.example",
                "sec-fetch-site": "cross-site",
            },
        )
        same_site = client.post(
            "/login",
            data={"email": "nobody@example.test", "password": "irrelevant"},
            headers={"origin": "http://testserver", "sec-fetch-site": "same-origin"},
        )

    assert blocked.status_code == 403
    assert blocked.text == "Cross-site write refused."
    assert same_site.status_code == 303


def test_api_cross_site_refusal_is_machine_readable():
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/imports/anything/commit",
            headers={"origin": "https://attacker.example", "sec-fetch-site": "cross-site"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_ORIGIN"


def test_backup_manifest_and_restore_round_trip(tmp_path):
    database = tmp_path / "live.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE specimen (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO specimen(value) VALUES ('before')")
    connection.commit()
    connection.close()

    backup = backup_sqlite(database, tmp_path / "backups")
    manifest = verify_backup(backup)
    assert manifest["sha256"]
    assert manifest["bytes"] > 0

    connection = sqlite3.connect(database)
    connection.execute("UPDATE specimen SET value = 'after'")
    connection.commit()
    connection.close()

    recovery = restore_sqlite(backup, database)
    assert recovery and recovery.is_file()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT value FROM specimen").fetchone()[0] == "before"
    finally:
        connection.close()


def test_release_check_is_non_mutating_and_ready():
    ok, report = run_checks()

    assert ok is True
    assert report["status"] == "ready"
    assert report["checks"]["assets"] == "ok"
    assert report["checks"]["integrity"] == "ok"


def test_compose_contract_has_persistent_data_and_a_readiness_probe():
    compose = yaml.safe_load((REPO / "compose.yaml").read_text(encoding="utf-8"))
    app_service = compose["services"]["app"]

    assert "rabta-data:/data" in app_service["volumes"]
    assert "rabta-backups:/backups" in app_service["volumes"]
    assert app_service["environment"]["AUTO_CREATE_SCHEMA"] == "false"
    assert app_service["environment"]["SYNTHETIC_DATA"] == "true"
    assert app_service["environment"]["QWEN_API_KEY"] == "${QWEN_API_KEY:-}"
    assert app_service["environment"]["AI_TIMEOUT_SECONDS"] == "${AI_TIMEOUT_SECONDS:-8}"
    assert "/health/ready" in " ".join(app_service["healthcheck"]["test"])

    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "USER rabta" in dockerfile
    assert "npm ci" in dockerfile
    assert "HEALTHCHECK" in dockerfile


def test_clean_environment_declares_the_complete_forecast_runtime():
    requirements = (REPO / "requirements.txt").read_text(encoding="utf-8")

    assert "lightgbm==" in requirements
    assert "scikit-learn==" in requirements
    assert LIGHTGBM_AVAILABLE is True


def test_onboarding_assets_and_persistence_are_part_of_the_release_contract():
    model = (REPO / "db" / "models.py").read_text(encoding="utf-8")
    shell = (REPO / "web" / "templates" / "layout" / "base.html").read_text(
        encoding="utf-8"
    )

    assert "preferences_json" in model
    assert "/app/getting-started" in shell
    assert "onboarding.help_tooltip" in shell


def test_example_environment_contains_no_real_secret():
    example = (REPO / ".env.example").read_text(encoding="utf-8")

    assert "SECRET_KEY=\n" in example
    assert "QWEN_API_KEY=\n" in example
    assert "APP_ENV=development" in example
