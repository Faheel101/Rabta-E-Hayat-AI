"""The donor register and the donor record, through the real app.

Donor identity is the most sensitive data this system holds, so the tenancy
tests here are not a formality — a leak would be a leak of names, phone numbers
and infection status.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text
from starlette.testclient import TestClient

from core import eligibility
from core.clock import DEMO_DATETIME
from db.models import Donor, Facility, UserAccount
from db.session import SessionLocal
from web.main import app

PASSWORD = "Rabta@2026"
OFFICER = "dr.ahmed@punjab-teaching.rabta.pk"
GROUP_USER = "s.fatima@punjab-teaching.rabta.pk"
OTHER_ORG = "a.hussain@shaukat-khanum.rabta.pk"


@pytest.fixture(scope="module")
def db():
    session = SessionLocal()

    try:
        yield session
    finally:
        session.close()


def make_client() -> TestClient:
    return TestClient(app, follow_redirects=True)


def sign_in(client: TestClient, email: str, password: str = PASSWORD):
    return client.post("/login", data={"email": email, "password": password})


def facility_of(db, email: str) -> str:
    return db.scalar(select(UserAccount.facility_id).where(UserAccount.email == email))


# ------------------------------------------------------------------- access


def test_register_requires_authentication():
    with make_client() as client:
        response = client.get("/app/donors")

        assert "login" in str(response.url).lower()


def test_register_loads_for_a_facility_user():
    with make_client() as client:
        sign_in(client, OFFICER)
        response = client.get("/app/donors")

        assert response.status_code == 200
        assert "Donors on register" in response.text


# ------------------------------------------------------------------ tenancy


def test_register_shows_only_this_facilitys_donors(db):
    """The header count must equal the register, not the whole province."""

    facility_id = facility_of(db, OFFICER)
    expected = db.scalar(
        select(func.count())
        .select_from(Donor)
        .where(Donor.registered_facility_id == facility_id)
    )

    with make_client() as client:
        sign_in(client, OFFICER)
        body = client.get("/app/donors").text

    assert f"{expected:,}" in body, (
        f"register should show {expected:,} donors for this facility"
    )


def test_a_donor_from_another_organization_is_not_reachable(db):
    """Changing the id in the URL must not read another tenant's donor. A 404
    rather than a 403, so the response does not confirm the record exists."""

    officer_org = db.scalar(
        select(UserAccount.organization_id).where(UserAccount.email == OFFICER)
    )
    foreign_facility = db.scalars(
        select(Facility).where(Facility.organization_id != officer_org).limit(1)
    ).first()
    foreign_donor = db.scalars(
        select(Donor)
        .where(Donor.registered_facility_id == foreign_facility.id)
        .limit(1)
    ).first()

    assert foreign_donor is not None, "no foreign donor to test against"

    with make_client() as client:
        sign_in(client, OFFICER)
        response = client.get(f"/app/donors/{foreign_donor.id}")

    assert response.status_code == 404


def test_two_organizations_see_different_registers(db):
    with make_client() as first, make_client() as second:
        sign_in(first, OFFICER)
        sign_in(second, OTHER_ORG)

        one = first.get("/app/donors").text
        two = second.get("/app/donors").text

    assert one != two, "two tenants rendered an identical register"


def test_a_group_coordinator_sees_the_whole_organizations_register(db):
    """A coordinator with no home facility must not get an empty page."""

    org_id = db.scalar(
        select(UserAccount.organization_id).where(UserAccount.email == GROUP_USER)
    )
    expected = db.scalar(
        select(func.count())
        .select_from(Donor)
        .join(Facility, Facility.id == Donor.registered_facility_id)
        .where(Facility.organization_id == org_id)
    )

    with make_client() as client:
        sign_in(client, GROUP_USER)
        body = client.get("/app/donors").text

    assert expected > 0
    assert f"{expected:,}" in body


# ------------------------------------------------------------------ filtering


def test_the_eligible_filter_excludes_permanently_deferred_donors(db):
    """Signed in as the group coordinator, whose scope is the whole
    organisation — permanent deferrals are rare enough that a single facility
    may have none, and a test that silently skips proves nothing."""

    org_id = db.scalar(
        select(UserAccount.organization_id).where(UserAccount.email == GROUP_USER)
    )

    permanent = db.scalars(
        select(Donor)
        .join(Facility, Facility.id == Donor.registered_facility_id)
        .where(
            Facility.organization_id == org_id,
            Donor.is_permanently_deferred.is_(True),
        )
        .limit(1)
    ).first()

    assert permanent is not None, (
        "no permanently deferred donor in this organisation to test against"
    )

    with make_client() as client:
        sign_in(client, GROUP_USER)
        body = client.get("/app/donors?availability=eligible").text

    assert permanent.id not in body, (
        "a permanently deferred donor appeared under 'eligible to donate today'"
    )


def test_the_eligible_filter_excludes_donors_inside_their_interval(db):
    """The interval is sex-dependent, which is exactly where an off-by-one
    lands a donor on the recall list a month too early."""

    facility_id = facility_of(db, OFFICER)
    male_days = eligibility.interval_days("MALE")

    recent_male = db.execute(
        text(
            "SELECT id FROM donor WHERE registered_facility_id = :f "
            "AND gender = 'MALE' AND last_donation_at IS NOT NULL "
            "AND JULIANDAY(:now) - JULIANDAY(last_donation_at) < :days LIMIT 1"
        ),
        {"f": facility_id, "now": DEMO_DATETIME, "days": male_days},
    ).first()

    if recent_male is None:
        pytest.skip("no donor inside the interval at this facility")

    with make_client() as client:
        sign_in(client, OFFICER)
        body = client.get("/app/donors?availability=eligible").text

    assert recent_male[0] not in body


def test_search_by_donor_code_finds_that_donor(db):
    facility_id = facility_of(db, OFFICER)
    donor = db.scalars(
        select(Donor).where(Donor.registered_facility_id == facility_id).limit(1)
    ).first()

    with make_client() as client:
        sign_in(client, OFFICER)
        body = client.get(f"/app/donors?q={donor.donor_code}").text

    assert donor.donor_code in body


# --------------------------------------------------------------- the record


def _donor_with(db, facility_id: str, donation_status: str):
    return db.execute(
        text(
            "SELECT dn.id FROM donor dn JOIN donation d ON d.donor_id = dn.id "
            "WHERE dn.registered_facility_id = :f AND d.status = :s LIMIT 1"
        ),
        {"f": facility_id, "s": donation_status},
    ).first()


def test_the_record_shows_the_whole_chain(db):
    """Donor, donation, test panel, unit — the point of the page."""

    row = _donor_with(db, facility_of(db, OFFICER), "RELEASED")

    if row is None:
        pytest.skip("no donor with a released donation")

    with make_client() as client:
        sign_in(client, OFFICER)
        body = client.get(f"/app/donors/{row[0]}").text

    for expected in ("Donation history", "Screening history", "HCV", "Released"):
        assert expected in body, f"the record is missing {expected!r}"


def test_a_reactive_donation_reads_as_quarantined_not_released(db):
    """If this ever renders as released, the page is telling a user a unit is
    safe to issue when the lab said otherwise."""

    row = _donor_with(db, facility_of(db, OFFICER), "QUARANTINED")

    if row is None:
        pytest.skip("no donor with a reactive donation")

    with make_client() as client:
        sign_in(client, OFFICER)
        body = client.get(f"/app/donors/{row[0]}").text

    assert "Quarantined" in body
    assert "reactive" in body


def test_the_record_never_prints_a_full_cnic(db):
    """Only the last four digits and a one-way hash are stored, and only the
    last four may be shown."""

    facility_id = facility_of(db, OFFICER)
    donor = db.scalars(
        select(Donor)
        .where(
            Donor.registered_facility_id == facility_id,
            Donor.cnic_hash.is_not(None),
        )
        .limit(1)
    ).first()

    if donor is None:
        pytest.skip("no donor with a CNIC on record")

    with make_client() as client:
        sign_in(client, OFFICER)
        body = client.get(f"/app/donors/{donor.id}").text

    assert donor.cnic_hash not in body, "the CNIC hash was rendered to the page"
    assert donor.cnic_last4 in body


def test_a_donor_who_withheld_contact_consent_has_no_phone_shown(db):
    facility_id = facility_of(db, OFFICER)
    donor = db.scalars(
        select(Donor)
        .where(
            Donor.registered_facility_id == facility_id,
            Donor.consent_contact.is_(False),
            Donor.phone.is_not(None),
        )
        .limit(1)
    ).first()

    if donor is None:
        pytest.skip("every donor at this facility consented to contact")

    with make_client() as client:
        sign_in(client, OFFICER)
        register = client.get(f"/app/donors?q={donor.donor_code}").text
        record = client.get(f"/app/donors/{donor.id}").text

    assert donor.phone not in register, "phone shown without consent on the register"
    assert donor.phone not in record, "phone shown without consent on the record"
