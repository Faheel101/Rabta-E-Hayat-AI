"""A deferral recorded in the database must be visible on the screen.

This is the defect class that has now appeared three times in this project, each
time in a new place: 9,988 screening deferrals that wrote nothing to the donor
record, then 1,683 donors holding an open deferral that the register scored
"Eligible". Both had the same shape — the ledger said one thing, the column the
page read said another.

`core/eligibility.py`'s module docstring names the exact failure: an engine
computing `eligible = today >= deferred_until` scores a currently-pregnant donor
as eligible today, because a conditional deferral has no end date. These tests
exist so that mistake cannot come back through the web layer.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, or_, select, text
from starlette.testclient import TestClient

from core.clock import DEMO_DATETIME
from db.models import Donor, DonorDeferral, Facility, UserAccount
from db.session import SessionLocal
from web.main import app
from web.routers.donors import active_deferrals, eligibility_state

PASSWORD = "Rabta@2026"
OFFICER = "dr.ahmed@punjab-teaching.rabta.pk"
GROUP_USER = "s.fatima@punjab-teaching.rabta.pk"


@pytest.fixture(scope="module")
def db():
    session = SessionLocal()

    try:
        yield session
    finally:
        session.close()


def make_client() -> TestClient:
    return TestClient(app, follow_redirects=True)


def sign_in(client: TestClient, email: str):
    return client.post("/login", data={"email": email, "password": PASSWORD})


def open_deferral_exists():
    """A deferral that has not been lifted and has not elapsed."""

    return (
        select(DonorDeferral.id)
        .where(
            DonorDeferral.donor_id == Donor.id,
            DonorDeferral.lifted_at.is_(None),
            or_(
                DonorDeferral.is_permanent.is_(True),
                DonorDeferral.deferred_until.is_(None),
                DonorDeferral.deferred_until > DEMO_DATETIME.date(),
            ),
        )
        .exists()
    )


# ------------------------------------------------------------- the verdict


def _first_with_status(db, status: str):
    return db.scalars(
        select(Donor).where(Donor.availability_status == status).limit(1)
    ).first()


@pytest.mark.parametrize(
    "status,expected",
    [
        ("CONDITIONALLY_DEFERRED", "CONDITIONAL"),
        ("AWAITING_TTI_CONFIRMATION", "AWAITING_CONFIRMATION"),
        ("PERMANENTLY_DEFERRED", "PERMANENT"),
    ],
)
def test_a_deferral_with_no_end_date_is_never_scored_eligible(db, status, expected):
    """The heart of it. These deferrals do not expire on a date, so a check
    against `deferred_until` alone cannot see them."""

    donor = _first_with_status(db, status)

    assert donor is not None, f"no donor in state {status} to test"

    deferrals = active_deferrals(db, [donor.id]).get(donor.id)
    state = eligibility_state(donor, today=DEMO_DATETIME.date(), deferrals=deferrals)

    assert state["code"] == expected, (
        f"a {status} donor was scored {state['code']} ({state['label']})"
    )
    assert state["tone"] != "success"


def test_the_eligible_filter_returns_nobody_with_an_open_deferral(db):
    """The SQL filter and the rendered verdict must agree. If they diverge, the
    header count promises a recall list that the rows contradict."""

    org_id = db.scalar(
        select(UserAccount.organization_id).where(UserAccount.email == GROUP_USER)
    )
    facility_ids = [
        row[0]
        for row in db.execute(
            select(Facility.id).where(Facility.organization_id == org_id)
        ).all()
    ]

    leaked = db.scalar(
        select(func.count())
        .select_from(Donor)
        .where(
            Donor.registered_facility_id.in_(facility_ids),
            Donor.is_permanently_deferred.is_(False),
            or_(
                Donor.deferred_until.is_(None),
                Donor.deferred_until <= DEMO_DATETIME.date(),
            ),
            open_deferral_exists(),
        )
    )

    assert leaked > 0, (
        "no donor holds an open deferral invisible to the donor columns, so this "
        "test cannot prove the filter consults the ledger"
    )

    with make_client() as client:
        sign_in(client, GROUP_USER)
        body = client.get("/app/donors?availability=eligible").text

    offenders = db.scalars(
        select(Donor.id)
        .where(
            Donor.registered_facility_id.in_(facility_ids),
            open_deferral_exists(),
        )
        .limit(200)
    ).all()

    shown = [donor_id for donor_id in offenders if donor_id in body]

    assert not shown, (
        f"{len(shown)} deferred donors appear under 'Eligible to donate today'"
    )


def test_a_deferred_donor_reads_as_deferred_on_their_record(db):
    # Scoped to the signed-in user's own organisation. Picking any donor in the
    # province gets a 404 from the tenancy guard, which would look like a bug in
    # the deferral display and is actually the guard doing its job.
    org_id = db.scalar(
        select(UserAccount.organization_id).where(UserAccount.email == GROUP_USER)
    )

    donor = db.scalars(
        select(Donor)
        .join(Facility, Facility.id == Donor.registered_facility_id)
        .where(
            Facility.organization_id == org_id,
            Donor.availability_status == "AWAITING_TTI_CONFIRMATION",
        )
        .limit(1)
    ).first()

    assert donor is not None, "no awaiting-confirmation donor in this organisation"

    with make_client() as client:
        sign_in(client, GROUP_USER)
        response = client.get(f"/app/donors/{donor.id}")

    assert response.status_code == 200
    assert "Awaiting confirmation" in response.text
    assert "Eligible</span>" not in response.text


# ------------------------------------------------ the denormalised column


def test_availability_status_agrees_with_the_deferral_ledger(db):
    """A denormalised column that drifts from its source is worse than no
    column: it looks authoritative while being wrong."""

    available_but_deferred = db.scalar(
        text(
            "SELECT COUNT(*) FROM donor dn WHERE dn.availability_status = 'AVAILABLE' "
            "  AND EXISTS (SELECT 1 FROM donor_deferral f "
            "              WHERE f.donor_id = dn.id AND f.lifted_at IS NULL "
            "                AND (f.is_permanent = 1 "
            "                     OR f.deferred_until IS NULL "
            "                     OR f.deferred_until > :today))"
        ),
        {"today": DEMO_DATETIME.date()},
    )

    assert available_but_deferred == 0, (
        f"{available_but_deferred:,} donors read AVAILABLE while holding an open deferral"
    )


def test_no_donor_is_marked_deferred_without_a_deferral_behind_it(db):
    """The other direction: a status that blocks donation with no record of why
    is a donor nobody can ever reinstate."""

    unexplained = db.scalar(
        text(
            "SELECT COUNT(*) FROM donor WHERE availability_status LIKE '%DEFERRED%' "
            "  AND is_permanently_deferred = 0 "
            "  AND deferred_until IS NULL "
            "  AND NOT EXISTS (SELECT 1 FROM donor_deferral f "
            "                  WHERE f.donor_id = donor.id AND f.lifted_at IS NULL)"
        )
    )

    assert unexplained == 0, (
        f"{unexplained:,} donors are blocked from donating with no deferral record"
    )


def test_an_elapsed_deferral_does_not_leave_a_stale_status(db):
    stale = db.scalar(
        text(
            "SELECT COUNT(*) FROM donor WHERE availability_status = 'TEMPORARILY_DEFERRED' "
            "  AND deferred_until IS NOT NULL AND deferred_until <= :today"
        ),
        {"today": DEMO_DATETIME.date()},
    )

    assert stale == 0


def test_every_availability_status_is_one_the_ui_can_render(db):
    """An undocumented status value renders as a blank chip, which reads to the
    user as 'no restriction'."""

    known = {
        "AVAILABLE",
        "TEMPORARILY_DEFERRED",
        "CONDITIONALLY_DEFERRED",
        "PERMANENTLY_DEFERRED",
        "AWAITING_TTI_CONFIRMATION",
        # Rendered as the donation-interval state from last_donation_at; this
        # explicit workflow status is not a blank or unrestricted state.
        "RECENTLY_DONATED",
    }

    seen = {
        row[0]
        for row in db.execute(
            select(Donor.availability_status).distinct()
        ).all()
        if row[0]
    }

    assert seen <= known, f"unhandled availability_status values: {seen - known}"
