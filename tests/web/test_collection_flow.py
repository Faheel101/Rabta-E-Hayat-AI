"""The collection module end to end, through the real app.

Runs against a copy of the database pointed at by DATABASE_URL, so the demo data
is untouched. The service layer commits deliberately — an audit entry and its
change share a transaction — so a session rollback would discard nothing.

What these cover that the service tests cannot: that the wizard's steps actually
reach the service, that the verdict a user sees is the one the engine produced,
and that the privacy and permission decisions hold in the rendered HTML rather
than only in the function that made them.
"""

from __future__ import annotations


import pytest

OFFICER = "dr.ahmed@punjab-teaching.rabta.pk"
PHLEBOTOMIST = "n.bibi@punjab-teaching.rabta.pk"
TECHNOLOGIST = "r.aslam@punjab-teaching.rabta.pk"
PASSWORD = "Rabta@2026"


@pytest.fixture(scope="module")
def client(scratch_path):
    """The app, with its database dependency pointed at a throwaway copy.

    An earlier version set DATABASE_URL and reloaded db.session. That does not
    isolate anything: every router did `from web.deps import get_db` at import
    time, so they kept the original SessionLocal and 21 donors, 3 donations and
    24 screenings leaked into the demo database.

    Overriding the dependency is the mechanism FastAPI provides for exactly
    this, and it does not care what order anything was imported in.
    """

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from starlette.testclient import TestClient

    from web.deps import get_db
    from web.main import app

    # The shared session-scoped copy. Made once per run with SQLite's backup
    # API — a plain file copy misses the WAL, and one copy per module of a
    # 966 MB database filled the disk.
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


def sign_in(client, email=OFFICER):
    return client.post("/login", data={"email": email, "password": PASSWORD})


@pytest.fixture
def camp(client):
    """An open outreach camp with a target, to collect against."""

    sign_in(client)

    response = client.post(
        "/app/sessions/open",
        data={
            "session_type": "OUTREACH_CAMP",
            "venue": "Government College",
            "organiser": "Pakistan Red Crescent Society",
            "target_units": "120",
        },
    )

    assert response.status_code == 200

    return str(response.url).rstrip("/").split("/")[-1]


# -------------------------------------------------------------------- sessions


def test_a_camp_needs_a_venue_and_says_so(client):
    sign_in(client)

    response = client.post(
        "/app/sessions/open", data={"session_type": "OUTREACH_CAMP", "venue": ""}
    )

    assert "venue" in response.text.lower()


def test_the_session_page_shows_the_target_it_was_opened_with(client, camp):
    body = client.get(f"/app/sessions/{camp}").text

    assert "120" in body
    assert "Government College" in body
    assert "Screened" in body and "Collected" in body


def test_closing_reports_the_gap_against_the_committed_target(client, camp):
    client.post(f"/app/sessions/{camp}/close")
    body = client.get(f"/app/sessions/{camp}").text

    assert "Closed" in body
    assert "short of its" in body, "a session closed under target does not say so"


# ---------------------------------------------------------------- the wizard


def _register_and_start(client, camp, **overrides):
    data = {
        "full_name": "Flow Test Donor",
        "gender": "MALE",
        "date_of_birth": "1995-04-12",
        "blood_group_id": "2",
        "phone": "0300-1112223",
        "donor_type": "VOLUNTARY",
        "session_id": camp,
    }
    data.update(overrides)

    response = client.post("/app/donors/register", data=data)

    assert response.status_code == 200

    return str(response.url).rstrip("/").split("/")[-1], response


def test_inline_registration_lands_straight_in_the_wizard(client, camp):
    """At a camp, sending somebody away to fill in a form loses the donation."""

    _, response = _register_and_start(client, camp)

    assert "Flow Test Donor" in response.text
    assert "Measurements" in response.text
    assert "History" in response.text


def test_the_verdict_updates_the_moment_a_measurement_is_saved(client, camp):
    """A donor deferred on their haemoglobin should learn that before answering
    twelve more questions."""

    screening_id, _ = _register_and_start(client, camp)

    deferred = client.post(
        f"/app/sessions/{camp}/screen/{screening_id}/vitals",
        data={"haemoglobin_g_dl": "9.4", "weight_kg": "70"},
    )

    assert "Deferred" in deferred.text

    accepted = client.post(
        f"/app/sessions/{camp}/screen/{screening_id}/vitals",
        data={
            "haemoglobin_g_dl": "15.1",
            "weight_kg": "72",
            "systolic_bp": "120",
            "diastolic_bp": "78",
            "pulse_bpm": "72",
            "temperature_c": "36.7",
        },
    )

    assert "Accepted" in accepted.text
    assert "450" in accepted.text, "the permitted collection volume is not shown"


def test_a_reduced_weight_shows_a_reduced_volume(client, camp):
    """The two-step ladder, visible at the chair rather than only in the record."""

    screening_id, _ = _register_and_start(client, camp)

    response = client.post(
        f"/app/sessions/{camp}/screen/{screening_id}/vitals",
        data={"haemoglobin_g_dl": "14.0", "weight_kg": "47"},
    )

    assert "350" in response.text
    assert "450" not in response.text.split("May collect")[1][:40]


def test_the_whole_chain_records_a_quarantined_unit(client, camp):
    screening_id, _ = _register_and_start(client, camp)

    client.post(
        f"/app/sessions/{camp}/screen/{screening_id}/vitals",
        data={
            "haemoglobin_g_dl": "15.1",
            "weight_kg": "72",
            "systolic_bp": "120",
            "diastolic_bp": "78",
            "pulse_bpm": "72",
            "temperature_c": "36.7",
        },
    )
    client.post(f"/app/sessions/{camp}/screen/{screening_id}/questions", data={})

    completed = client.post(
        f"/app/sessions/{camp}/screen/{screening_id}/complete", data={"notes": ""}
    )

    assert "Record the collection" in completed.text
    assert "quarantined" in completed.text

    collected = client.post(
        f"/app/sessions/{camp}/screen/{screening_id}/collect",
        data={
            "bag_type": "TRIPLE",
            "donation_type": "WHOLE_BLOOD",
            "adverse_reaction": "",
        },
    )

    assert "Who is in the chair" in collected.text, (
        "after a collection the wizard should return to the next donor"
    )


def test_a_deferred_donor_is_offered_no_collection_step(client, camp):
    """The refusal is in the service, but the screen must not offer it either."""

    screening_id, _ = _register_and_start(client, camp)

    client.post(
        f"/app/sessions/{camp}/screen/{screening_id}/vitals",
        data={"haemoglobin_g_dl": "9.2", "weight_kg": "70"},
    )
    completed = client.post(
        f"/app/sessions/{camp}/screen/{screening_id}/complete", data={"notes": ""}
    )

    assert "Deferred — no collection" in completed.text
    assert "Record the collection" not in completed.text


# ------------------------------------------------------------------- privacy


def test_the_session_page_never_names_a_deferred_donor(client, camp):
    """A camp session page is the screen most likely to be shown to an organiser
    or a volunteer, and who was deferred for what is a health finding."""

    screening_id, _ = _register_and_start(
        client, camp, full_name="Privacy Test Donor"
    )

    client.post(
        f"/app/sessions/{camp}/screen/{screening_id}/vitals",
        data={"haemoglobin_g_dl": "9.2", "weight_kg": "70"},
    )
    client.post(f"/app/sessions/{camp}/screen/{screening_id}/complete", data={})

    body = client.get(f"/app/sessions/{camp}").text

    assert "Privacy Test Donor" not in body

    # The template title-cases the code and strips the underscores, so match on
    # the rendered words rather than the stored constant.
    assert "haemoglobin" in body.lower(), (
        "the deferral reason should still be visible in aggregate"
    )


def test_an_unfinished_screening_does_name_the_donor_so_it_can_be_resumed(
    client, camp
):
    """The single exception, and the reason drafts are stored at all."""

    _register_and_start(client, camp, full_name="Resume Test Donor")

    body = client.get(f"/app/sessions/{camp}").text

    assert "Unfinished screenings" in body
    assert "Resume Test Donor" in body


def test_a_draft_is_not_counted_as_a_screening_on_the_session(client, camp):
    before = client.get(f"/app/sessions/{camp}").text
    _register_and_start(client, camp, full_name="Uncounted Donor")
    after = client.get(f"/app/sessions/{camp}").text

    # The Screened KPI must not have moved.
    import re

    def screened(html):
        match = re.search(r"Screened.*?kpi-value[^>]*>\s*([\d,]+)", html, re.S)
        return match.group(1) if match else None

    assert screened(before) == screened(after)


# --------------------------------------------------------------- permissions


def test_only_signoff_holders_see_the_queue_in_the_navigation(client):
    sign_in(client, OFFICER)
    officer_nav = client.get("/app/dashboard").text

    sign_in(client, PHLEBOTOMIST)
    phleb_nav = client.get("/app/dashboard").text

    sign_in(client, TECHNOLOGIST)
    lab_nav = client.get("/app/dashboard").text

    assert 'href="/app/signoff"' in officer_nav
    assert 'href="/app/signoff"' not in phleb_nav, (
        "a phlebotomist is shown a link they cannot use"
    )
    assert 'href="/app/signoff"' not in lab_nav


def test_the_signoff_queue_shows_both_limbs_of_the_disagreement(client):
    """A reviewer who only sees the answer the config picked cannot weigh
    anything."""

    sign_in(client, OFFICER)
    body = client.get("/app/signoff").text

    assert "Currently applied" in body
    assert "The alternative" in body


def test_a_phlebotomist_can_still_reach_the_collection_flow(client):
    sign_in(client, PHLEBOTOMIST)
    response = client.get("/app/sessions")

    assert response.status_code == 200
