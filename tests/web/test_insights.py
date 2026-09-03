"""Sprint 2 decision-intelligence routes and their safety boundaries."""

from __future__ import annotations

from sqlalchemy import select
from starlette.testclient import TestClient

from db.models import Facility, UserAccount
from db.session import SessionLocal
from web.main import app

PASSWORD = "Rabta@2026"
OFFICER = "dr.ahmed@punjab-teaching.rabta.pk"
COORDINATOR = "s.fatima@punjab-teaching.rabta.pk"
PHLEBOTOMIST = "n.bibi@punjab-teaching.rabta.pk"


def client() -> TestClient:
    return TestClient(app, follow_redirects=True)


def sign_in(web: TestClient, email: str = OFFICER):
    response = web.post("/login", data={"email": email, "password": PASSWORD})
    assert response.status_code == 200


def test_command_centre_is_a_decision_surface_not_a_placeholder():
    with client() as web:
        sign_in(web)
        response = web.get("/insights/command-centre")

    assert response.status_code == 200
    assert "Your four-minute morning read" in response.text
    assert "Forecast quality gates" in response.text
    assert "Inventory heatmap" in response.text
    assert "/insights/forecast?facility_id=" in response.text
    assert "[cc." not in response.text


def test_forecast_supports_chart_table_and_compare_views():
    with client() as web:
        sign_in(web)
        chart = web.get("/insights/forecast")
        table = web.get("/insights/forecast?view=table&horizon=30")
        compare = web.get("/insights/forecast?view=compare")

    assert chart.status_code == table.status_code == compare.status_code == 200
    assert "P10–P90" in chart.text
    assert "Projected stock position" in chart.text
    assert "WAPE (7-day)" in chart.text
    assert "Forecast quantiles" in table.text
    assert "P10 ≤ P50 ≤ P90" in table.text
    assert "All components and blood groups" in compare.text


def test_expiry_rescue_states_reason_destination_deadline_and_human_control():
    with client() as web:
        sign_in(web, COORDINATOR)
        response = web.get("/insights/expiry-rescue?tier=ACT_NOW")

    assert response.status_code == 200
    assert "Prioritised unit queue" in response.text
    assert "Waste probability" in response.text
    assert "Best destination" in response.text
    assert "Dispatch by" in response.text
    assert "Human approval required" in response.text
    assert "never authorizes movement of blood" in response.text


def test_expiry_rescue_defaults_to_actionable_and_links_into_approval_workflow():
    with client() as web:
        sign_in(web, COORDINATOR)
        response = web.get("/insights/expiry-rescue")

    assert response.status_code == 200
    assert '<option value="ACTIONABLE" selected>Needs action</option>' in response.text
    assert "How rescue decisions become safe movements" in response.text
    assert "Approve, modify or reject" in response.text
    assert "/insights/transfer-plan?status=RECOMMENDED" in response.text
    assert "Review approvals" in response.text
    assert "Review and decide" in response.text
    assert "Proposed plan" in response.text


def test_intelligence_filter_forms_accept_their_blank_all_options():
    with client() as web:
        sign_in(web, COORDINATOR)
        expiry = web.get(
            "/insights/expiry-rescue?facility_id=&component_id=&tier=WATCH&sort=deadline"
        )
        expiry_all = web.get(
            "/insights/expiry-rescue?facility_id=&component_id=&tier=&sort=deadline"
        )
        forecast = web.get(
            "/insights/forecast?facility_id=&component_id=&blood_group_id=&horizon=14&view=chart"
        )

    assert expiry.status_code == 200
    assert "Prioritised unit queue" in expiry.text
    assert '<option value="" selected>All</option>' in expiry_all.text
    assert forecast.status_code == 200
    assert "Demand Forecast" in forecast.text


def test_foreign_facility_ids_are_refused_on_every_intelligence_route():
    db = SessionLocal()
    try:
        user = db.scalar(select(UserAccount).where(UserAccount.email == OFFICER))
        foreign = db.scalar(
            select(Facility).where(
                Facility.organization_id != user.organization_id,
                Facility.organization_id.is_not(None),
            )
        )
    finally:
        db.close()

    with client() as web:
        sign_in(web)
        for path in (
            f"/insights/command-centre?facility_id={foreign.id}",
            f"/insights/forecast?facility_id={foreign.id}",
            f"/insights/expiry-rescue?facility_id={foreign.id}",
        ):
            response = web.get(path)
            assert response.status_code == 404, path


def test_bench_roles_cannot_open_planning_intelligence():
    with client() as web:
        sign_in(web, PHLEBOTOMIST)
        response = web.get("/insights/forecast")
        dashboard = web.get("/app/dashboard")

    assert response.status_code == 403
    assert "Demand Forecast" not in dashboard.text.split(
        'aria-label="Main navigation"'
    )[1].split("</nav>")[0]


def test_sprint_two_surfaces_are_native_rtl_not_english_fallbacks():
    with client() as web:
        sign_in(web)
        web.post(
            "/app/language",
            data={"lang": "ur", "next": "/insights/command-centre"},
        )
        command = web.get("/insights/command-centre")
        forecast = web.get("/insights/forecast")
        expiry = web.get("/insights/expiry-rescue")

    assert 'dir="rtl"' in command.text
    assert "چار منٹ" in command.text
    assert "طلب کی پیش گوئی" in forecast.text
    assert "انقضا سے بچاؤ" in expiry.text
    assert "[cc." not in command.text
    assert "[fc." not in forecast.text
    assert "[ex." not in expiry.text


def test_emergency_twin_and_alert_queue_are_integrated_decision_surfaces():
    with client() as web:
        sign_in(web, COORDINATOR)
        simulator = web.get("/insights/simulator")
        alerts = web.get("/insights/alerts")

    assert simulator.status_code == alerts.status_code == 200
    assert "SIMULATION MODE" in simulator.text
    assert "Scenario builder" in simulator.text
    assert "Emergency Digital Twin" in simulator.text
    assert "Accountable alert queue" in alerts.text
    assert "Refresh from evidence" in alerts.text
    assert "[sim." not in simulator.text
    assert "[alerts." not in alerts.text


def test_blood_bank_officer_has_view_only_emergency_twin_access():
    with client() as web:
        sign_in(web, OFFICER)
        response = web.get("/insights/simulator")

    assert response.status_code == 200
    assert "can review prepared results but cannot run or declare" in response.text
    assert "Run and save scenario" not in response.text


def test_emergency_and_alert_surfaces_are_native_rtl():
    with client() as web:
        sign_in(web, COORDINATOR)
        web.post(
            "/app/language",
            data={"lang": "ur", "next": "/insights/simulator"},
        )
        simulator = web.get("/insights/simulator")
        alerts = web.get("/insights/alerts")

    assert simulator.status_code == alerts.status_code == 200
    assert 'dir="rtl"' in simulator.text
    assert "ہنگامی ڈیجیٹل ٹوئن" in simulator.text
    assert "جواب دہ الرٹ قطار" in alerts.text
    assert "[sim." not in simulator.text
    assert "[alerts." not in alerts.text
