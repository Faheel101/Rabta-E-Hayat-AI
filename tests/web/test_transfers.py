"""Sprint 3 transfer workspace, tenancy, RTL and printable custody artifacts."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from db.models import BloodUnit, Facility, Transfer, UserAccount
from db.session import SessionLocal
from services import transfer_service
from services.audit import Actor, ServiceError
from web.deps import get_db
from web.main import app

PASSWORD = "Rabta@2026"
COORDINATOR = "s.fatima@punjab-teaching.rabta.pk"
OFFICER = "dr.ahmed@punjab-teaching.rabta.pk"
OTHER_ORG = "a.hussain@shaukat-khanum.rabta.pk"
PHLEBOTOMIST = "n.bibi@punjab-teaching.rabta.pk"


def _client():
    return TestClient(app, follow_redirects=True)


def _sign_in(client, email=COORDINATOR):
    response = client.post("/login", data={"email": email, "password": PASSWORD})
    assert response.status_code == 200


def _punjab_transfer(db, *, source_only=False, status=None):
    user = db.scalar(select(UserAccount).where(UserAccount.email == COORDINATOR))
    facility_ids = list(
        db.scalars(select(Facility.id).where(Facility.organization_id == user.organization_id))
    )
    condition = Transfer.from_facility_id.in_(facility_ids)
    if not source_only:
        condition = condition | Transfer.to_facility_id.in_(facility_ids)
    if status:
        condition = condition & (Transfer.status == status)
    return db.scalars(select(Transfer).where(condition).order_by(Transfer.units.desc())).first()


def test_transfer_plan_is_an_execution_workspace_not_a_placeholder():
    with _client() as web:
        _sign_in(web)
        response = web.get("/insights/transfer-plan")

    assert response.status_code == 200
    assert "Governed execution queue" in response.text
    assert "Pending approval" in response.text
    assert "human decision required" in response.text
    assert "Open transfer" in response.text
    assert "/insights/transfer-plan/" in response.text
    assert "[tr." not in response.text


def test_detail_shows_evidence_manifest_cold_chain_and_accountable_actions():
    db = SessionLocal()
    try:
        transfer = _punjab_transfer(db, source_only=True, status="RECOMMENDED")
        din = db.get(BloodUnit, transfer.unit_ids[0]).din
    finally:
        db.close()

    with _client() as web:
        _sign_in(web)
        response = web.get(f"/insights/transfer-plan/{transfer.id}")

    assert response.status_code == 200
    assert "Decision evidence" in response.text
    assert "Physical unit manifest" in response.text
    assert "Cold-chain envelope" in response.text
    assert "Accountable approval" in response.text
    assert "Approve and reserve manifest" in response.text
    assert din in response.text


def test_transfer_detail_is_tenant_scoped_and_bench_roles_are_refused():
    db = SessionLocal()
    try:
        transfer = _punjab_transfer(db, source_only=True)
    finally:
        db.close()

    with _client() as web:
        _sign_in(web, OTHER_ORG)
        foreign = web.get(f"/insights/transfer-plan/{transfer.id}")

    with _client() as web:
        _sign_in(web, PHLEBOTOMIST)
        forbidden = web.get("/insights/transfer-plan")
        dashboard = web.get("/app/dashboard")

    assert foreign.status_code == 404
    assert forbidden.status_code == 403
    navigation = dashboard.text.split('aria-label="Main navigation"')[1].split("</nav>")[0]
    assert "Transfer Plan" not in navigation


def test_transfer_workspace_is_native_rtl_urdu():
    db = SessionLocal()
    try:
        transfer = _punjab_transfer(db, source_only=True)
    finally:
        db.close()

    with _client() as web:
        _sign_in(web, COORDINATOR)
        web.post(
            "/app/language",
            data={"lang": "ur", "next": f"/insights/transfer-plan/{transfer.id}"},
        )
        response = web.get(f"/insights/transfer-plan/{transfer.id}")

    assert response.status_code == 200
    assert 'dir="rtl"' in response.text
    assert "فیصلے کے شواہد" in response.text
    assert "حقیقی یونٹس کی فہرست" in response.text
    assert "[tr." not in response.text


def test_approved_transfer_prints_real_din_barcodes_and_tracking_qr(scratch_database):
    def override_db():
        db = Session(bind=scratch_database, expire_on_commit=False)
        try:
            yield db
        finally:
            db.close()

    setup = Session(bind=scratch_database, expire_on_commit=False)
    try:
        user = setup.scalar(select(UserAccount).where(UserAccount.email == COORDINATOR))
        facility_ids = list(
            setup.scalars(select(Facility.id).where(Facility.organization_id == user.organization_id))
        )
        candidates = setup.scalars(
            select(Transfer).where(
                Transfer.status == "RECOMMENDED",
                Transfer.from_facility_id.in_(facility_ids),
            )
        ).all()
        actor = Actor(
            user.id,
            user.full_name,
            user.role,
            organization_id=user.organization_id,
            organization_wide=True,
        )
        approved = None
        for candidate in candidates:
            try:
                approved = transfer_service.approve_transfer(
                    setup, actor, candidate.id
                )
                break
            except ServiceError:
                setup.rollback()
        assert approved is not None
        tracking_code = approved.tracking_code
        din = setup.get(BloodUnit, approved.unit_ids[0]).din
        transfer_id = approved.id
    finally:
        setup.close()

    app.dependency_overrides[get_db] = override_db
    try:
        with _client() as web:
            _sign_in(web)
            slip = web.get(f"/insights/transfer-plan/{transfer_id}/dispatch-slip")
            tracking = web.get(f"/insights/transfer-plan/track/{tracking_code}")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert slip.status_code == 200
    assert "data:image/svg+xml;base64," in slip.text
    assert din in slip.text
    assert "Scan to verify custody" in slip.text
    assert tracking.status_code == 200
    assert "Shipment tracking" in tracking.text
    assert tracking_code in tracking.text
