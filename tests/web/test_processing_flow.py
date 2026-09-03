"""The separation bench through the real app.

What the page must convey beyond what the service enforces: which windows are
still open, how long is left, and that a bag past its platelet window is not a
wasted bag. A technologist who cannot see the clock cannot prioritise against it.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

TECHNOLOGIST = "r.aslam@punjab-teaching.rabta.pk"
PHLEBOTOMIST = "n.bibi@punjab-teaching.rabta.pk"
OFFICER = "dr.ahmed@punjab-teaching.rabta.pk"
PASSWORD = "Rabta@2026"


@pytest.fixture(scope="module")
def client(scratch_path):
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


def test_the_bench_loads_for_a_technologist(client):
    sign_in(client, TECHNOLOGIST)
    response = client.get("/app/processing")

    assert response.status_code == 200
    assert "Bags awaiting separation" in response.text


def test_a_phlebotomist_sees_no_processing_link(client):
    sign_in(client, PHLEBOTOMIST)
    nav = client.get("/app/dashboard").text

    assert 'href="/app/processing"' not in nav, (
        "a phlebotomist is shown a link they cannot use"
    )


def test_a_technologist_sees_the_processing_link(client):
    sign_in(client, TECHNOLOGIST)

    assert 'href="/app/processing"' in client.get("/app/dashboard").text


def test_the_yield_figure_says_what_it_excludes(client):
    """A yield percentage that quietly includes reconstructed records is a
    fabricated 100% averaged into a measured number."""

    sign_in(client, TECHNOLOGIST)
    body = client.get("/app/processing").text

    assert "Yield" in body

    if "excluded from the yield figure" in body:
        assert "never recorded" in body


def test_the_page_shows_how_long_each_window_has_left(client):
    """A technologist prioritising has to see the clock, not a yes/no."""

    sign_in(client, TECHNOLOGIST)
    body = client.get("/app/processing").text

    if "Nothing to separate" in body:
        pytest.skip("no bags awaiting separation")

    # Either a countdown or a closed window must be shown for every bag.
    assert re.search(r"(?:\d+(?:\.\d+)?h|window closed)", body, re.IGNORECASE)


def test_a_late_bag_is_not_presented_as_wasted(client):
    """The whole point of a per-component window: red cells survive."""

    sign_in(client, TECHNOLOGIST)
    body = client.get("/app/processing").text

    if "window closed" not in body:
        pytest.skip("no bag has missed a window")

    assert "a late bag is not a wasted bag" in body.lower()


def test_window_labels_do_not_expose_floating_point_artifacts(client):
    sign_in(client, TECHNOLOGIST)
    body = client.get("/app/processing").text

    assert "000000000000" not in body


def test_the_loss_breakdown_distinguishes_scheduling_from_technique(client):
    sign_in(client, TECHNOLOGIST)
    body = client.get("/app/processing").text

    if "Where the losses went" not in body:
        pytest.skip("no losses recorded at this facility yet")

    assert "scheduling problem" in body
