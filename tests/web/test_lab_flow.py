"""The lab bench through the real app: worklist, plate, release.

The point of the pages, over and above the service tests, is that the two-person
rule is VISIBLE. A donation the signed-in technologist tested is shown with the
reason it cannot be signed here, rather than quietly filtered out — a control
nobody can see is one people work around.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

OFFICER = "dr.ahmed@punjab-teaching.rabta.pk"
TECH_A = "r.aslam@punjab-teaching.rabta.pk"
TECH_B = "f.noor@punjab-teaching.rabta.pk"
PHLEBOTOMIST = "n.bibi@punjab-teaching.rabta.pk"
PASSWORD = "Rabta@2026"


@pytest.fixture(scope="module")
def client(scratch_path):
    """The app, with its database dependency pointed at the shared scratch copy."""

    from starlette.testclient import TestClient

    from web.deps import get_db
    from web.main import app

    engine = create_engine(
        f"sqlite:///{scratch_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    ScratchSession = sessionmaker(bind=engine, expire_on_commit=False)

    def scratch_db():
        session = ScratchSession()

        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = scratch_db

    try:
        with TestClient(app, follow_redirects=True) as session:
            yield session
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def sign_in(client, email):
    return client.post("/login", data={"email": email, "password": PASSWORD})


# ------------------------------------------------------------------ the bench


def test_the_bench_shows_the_quarantine_shelf(client):
    """Donations collected after the last plate are waiting on the next one."""

    sign_in(client, TECH_A)
    body = client.get("/app/lab").text

    assert "Awaiting results" in body
    assert "Ready to release" in body


def test_a_phlebotomist_sees_no_lab_link(client):
    sign_in(client, PHLEBOTOMIST)
    nav = client.get("/app/dashboard").text

    assert 'href="/app/lab"' not in nav, (
        "a phlebotomist is shown a lab link they cannot use"
    )


def test_a_technologist_sees_the_lab_link(client):
    sign_in(client, TECH_A)
    nav = client.get("/app/dashboard").text

    assert 'href="/app/lab"' in nav


# -------------------------------------------------------------------- plates


def _open_plate(client, test_code="HIV", **overrides):
    data = {
        "test_code": test_code,
        "kit_lot": "LOT-FLOW-1",
        "kit_expiry": "",
        "equipment": "Evolis 1",
    }
    data.update(overrides)

    response = client.post("/app/lab/runs/open", data=data)

    assert response.status_code == 200

    return str(response.url).rstrip("/").split("/")[-1], response


def test_opening_a_plate_lands_on_it(client):
    sign_in(client, TECH_A)
    _, response = _open_plate(client)

    assert "Samples awaiting HIV" in response.text
    assert "LOT-FLOW-1" in response.text


def test_an_expired_kit_is_refused_with_a_reason(client):
    sign_in(client, TECH_A)

    response = client.post(
        "/app/lab/runs/open",
        data={"test_code": "HIV", "kit_lot": "OLD", "kit_expiry": "2020-01-01"},
    )

    assert "expired" in response.text.lower()


def test_a_plate_asks_for_its_controls_before_anything_else(client):
    sign_in(client, TECH_A)
    _, response = _open_plate(client, test_code="HCV")

    assert "Controls" in response.text
    assert "Controls not recorded" in response.text


def test_failed_controls_invalidate_the_plate_visibly(client):
    sign_in(client, TECH_A)
    run_id, _ = _open_plate(client, test_code="SYPHILIS")

    response = client.post(
        f"/app/lab/runs/{run_id}/controls",
        data={"controls_valid": "0", "note": "Positive control flat."},
    )

    assert "invalidated" in response.text.lower()
    assert "Positive control flat." in response.text
    assert "must be re-run" in response.text


def test_recording_a_reactive_result_says_what_it_did(client):
    """The cascade is not silent — the user is told the units were discarded and
    the donor deferred, because they are the ones who will be asked."""

    sign_in(client, TECH_A)

    pending = client.get("/app/lab").text

    if "Awaiting results" not in pending:
        pytest.skip("nothing on the worklist")

    run_id, plate = _open_plate(client, test_code="MALARIA")

    import re

    donation_ids = re.findall(r'name="result_([0-9a-f-]{36})"', plate.text)

    if not donation_ids:
        pytest.skip("no samples awaiting this marker")

    response = client.post(
        f"/app/lab/runs/{run_id}/results",
        data={f"result_{donation_ids[0]}": "REACTIVE"},
    )

    text = response.text.lower()

    assert "reactive" in text
    assert "discarded" in text or "deferred" in text


# ------------------------------------------------------------------- release


def _fully_test(client, donation_id):
    """Run the whole panel against one donation, as technologist A."""

    from services import lab

    for marker in lab.required_tests():
        run_id, _ = _open_plate(client, test_code=marker, kit_lot=f"LOT-{marker}")
        client.post(
            f"/app/lab/runs/{run_id}/results",
            data={f"result_{donation_id}": "NON_REACTIVE"},
        )


def _a_pending_donation(client):
    import re

    body = client.get("/app/lab").text
    ids = re.findall(r"/app/lab/release/([0-9a-f-]{36})", body)

    if ids:
        return ids[0]

    run_id, plate = _open_plate(client, test_code="HIV", kit_lot="LOT-PICK")
    found = re.findall(r'name="result_([0-9a-f-]{36})"', plate.text)

    return found[0] if found else None


def test_the_two_person_rule_is_shown_not_hidden(client):
    """A donation this user tested appears with the reason it cannot be signed
    here. Hiding it would make the control invisible."""

    sign_in(client, TECH_A)

    donation_id = _a_pending_donation(client)

    if donation_id is None:
        pytest.skip("no donation available to test")

    _fully_test(client, donation_id)

    body = client.get("/app/lab").text

    assert "You tested this" in body, (
        "the two-person rule is being enforced by hiding rather than explaining"
    )
    assert "Blocked by two-person rule" in body


def test_the_tester_cannot_release_their_own_work(client):
    sign_in(client, TECH_A)

    donation_id = _a_pending_donation(client)

    if donation_id is None:
        pytest.skip("no donation available to test")

    _fully_test(client, donation_id)

    response = client.post(f"/app/lab/release/{donation_id}")

    assert "cannot also release" in response.text or "second person" in response.text


def test_a_second_technologist_can_release_it(client):
    sign_in(client, TECH_A)

    donation_id = _a_pending_donation(client)

    if donation_id is None:
        pytest.skip("no donation available to test")

    _fully_test(client, donation_id)

    sign_in(client, TECH_B)
    response = client.post(f"/app/lab/release/{donation_id}")

    assert "released" in response.text.lower()
    assert "available stock" in response.text.lower()
