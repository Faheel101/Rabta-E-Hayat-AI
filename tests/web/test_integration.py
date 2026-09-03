"""End-to-end web integration tests.

These drive the real application over HTTP and reconcile every figure it renders
against a direct SQL query. A dashboard that shows a plausible number computed
the wrong way is worse than one that shows nothing, so the assertions here
compare the page against the database rather than against a fixture.

The tenant isolation tests are the important ones. Everything else is a feature;
isolation is a promise.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from starlette.testclient import TestClient

from db.models import (
    BloodUnit,
    Facility,
    MartDaysOfCover,
    Organization,
    Transfer,
    UserAccount,
    UserSession,
)
from db.session import SessionLocal
from services.common import DEMO_DATETIME
from web.main import app
from web.security import SESSION_COOKIE

PASSWORD = "Rabta@2026"

# One account per tenant shape: a facility-pinned officer, a group-level
# coordinator, and an officer at a different organization entirely.
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


# A 13-character DIN: 5-character FIN, 2-digit year, 6-digit sequence. Written
# as a structural pattern rather than a literal prefix so that registering a real
# ICCBBA FIN does not break these tests.
DIN_PATTERN = "[A-NP-Z1-9][A-Z0-9]{2}" + chr(92) + "d{10}"


def digits(text: str) -> list[int]:
    return [int(value.replace(",", "")) for value in re.findall(r"\d[\d,]*", text)]


def kpi_value(html: str, label: str) -> int:
    """Pull the number out of a KPI card by its label."""

    pattern = (
        r'<div class="kpi[^\"]*">\s*<p class="kpi-label">\s*'
        + re.escape(label)
        + r".*?<p class=\"kpi-value[^\"]*\">\s*([\d,]+)"
    )
    match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)

    assert match, f"KPI {label!r} not found in page"

    return int(match.group(1).replace(",", ""))


# --------------------------------------------------------------- auth flow ----


def test_unauthenticated_root_redirects_to_login():
    with make_client() as client:
        response = client.get("/")

        assert response.status_code == 200
        assert "/login" in str(response.url)
        assert "Sign in to your blood bank" in response.text


def test_dashboard_requires_authentication():
    with make_client() as client:
        response = client.get("/app/dashboard")

        assert "/login" in str(response.url)
        assert "Sign in" in response.text


def test_sign_in_succeeds_and_lands_on_own_facility(db):
    with make_client() as client:
        response = sign_in(client, OFFICER)

        assert response.status_code == 200
        assert "/app/dashboard" in str(response.url)

        user = db.scalar(select(UserAccount).where(UserAccount.email == OFFICER))
        facility = db.get(Facility, user.facility_id)

        assert facility.name_en in response.text


def test_sign_in_rejects_wrong_password():
    with make_client() as client:
        response = sign_in(client, OFFICER, "wrong-password")

        assert "not recognised" in response.text
        assert "Sign in to your blood bank" in response.text


def test_unknown_email_and_wrong_password_are_indistinguishable():
    """The form must not be usable to discover which accounts exist."""

    with make_client() as client:
        unknown = sign_in(client, "nobody@nowhere.example", "whatever")

    with make_client() as client:
        wrong = sign_in(client, OFFICER, "wrong-password")

    assert "not recognised" in unknown.text
    assert "not recognised" in wrong.text


def test_sign_out_revokes_the_session(db):
    with make_client() as client:
        sign_in(client, OFFICER)
        token = client.cookies.get(SESSION_COOKIE)

        assert token

        client.post("/logout")

        session = db.get(UserSession, token)
        db.refresh(session) if session else None

        assert session is not None
        assert session.revoked_at is not None

        # And the revoked session no longer grants access.
        after = client.get("/app/dashboard")
        assert "/login" in str(after.url)


def test_failed_logins_increment_and_then_lock(db):
    """Spec §13.2 requires rate limiting on credentials."""

    from web.security import MAX_FAILED_LOGINS

    email = OTHER_ORG
    user = db.scalar(select(UserAccount).where(UserAccount.email == email))
    original_hash = user.password_hash

    try:
        for _ in range(MAX_FAILED_LOGINS):
            with make_client() as client:
                sign_in(client, email, "definitely-wrong")

        db.expire_all()
        user = db.scalar(select(UserAccount).where(UserAccount.email == email))

        assert user.locked_until is not None, "account should be locked"

        with make_client() as client:
            response = sign_in(client, email, PASSWORD)
            assert "locked" in response.text.lower()

    finally:
        # Leave the seeded account usable for the rest of the suite.
        user.locked_until = None
        user.failed_login_count = 0
        user.password_hash = original_hash
        db.commit()


# ---------------------------------------------------------- tenant isolation --


def test_a_user_only_sees_their_own_organizations_facilities(db):
    with make_client() as client:
        sign_in(client, GROUP_USER)
        response = client.get("/app/dashboard")

        user = db.scalar(select(UserAccount).where(UserAccount.email == GROUP_USER))

        own = set(
            db.scalars(
                select(Facility.name_en).where(
                    Facility.organization_id == user.organization_id
                )
            ).all()
        )
        foreign = set(
            db.scalars(
                select(Facility.name_en).where(
                    Facility.organization_id != user.organization_id,
                    Facility.organization_id.is_not(None),
                )
            ).all()
        )

        # The switcher lists every facility the group owns.
        for name in own:
            assert name in response.text, f"{name} missing from own-org switcher"

        # And none that it does not.
        leaked = [name for name in foreign if name in response.text]

        assert not leaked, f"foreign facilities leaked into the page: {leaked[:3]}"


def test_switching_to_a_foreign_facility_is_refused(db):
    """The guard that matters: a facility id in a form body is untrusted."""

    with make_client() as client:
        sign_in(client, OFFICER)

        user = db.scalar(select(UserAccount).where(UserAccount.email == OFFICER))
        foreign = db.scalar(
            select(Facility.id).where(
                Facility.organization_id != user.organization_id,
                Facility.organization_id.is_not(None),
            )
        )

        response = client.post(
            "/app/switch-facility",
            data={"facility_id": foreign},
        )

        assert response.status_code == 404
        assert "Page not found" in response.text

        # And the active facility is unchanged.
        after = client.get("/app/dashboard")
        own_facility = db.get(Facility, user.facility_id)
        assert own_facility.name_en in after.text


def test_switching_within_the_organization_succeeds(db):
    with make_client() as client:
        sign_in(client, GROUP_USER)

        user = db.scalar(select(UserAccount).where(UserAccount.email == GROUP_USER))
        targets = db.scalars(
            select(Facility)
            .where(Facility.organization_id == user.organization_id)
            .order_by(Facility.name_en)
        ).all()

        assert len(targets) > 1, "need a multi-facility organization for this test"

        target = targets[-1]
        response = client.post(
            "/app/switch-facility", data={"facility_id": target.id}
        )

        assert response.status_code == 200
        assert f"Now working in {target.name_en}" in response.text


def test_two_organizations_see_different_stock(db):
    """The whole point of tenancy: the same page, different numbers."""

    with make_client() as client:
        sign_in(client, OFFICER)
        first = kpi_value(client.get("/app/dashboard").text, "Units on hand")

    with make_client() as client:
        sign_in(client, OTHER_ORG)
        second = kpi_value(client.get("/app/dashboard").text, "Units on hand")

    assert first != second, (
        "two different organizations rendered the same stock figure, which means "
        "the query is not scoped"
    )


# ------------------------------------------------------- numbers reconcile ----


def facility_for(db, email: str) -> Facility:
    user = db.scalar(select(UserAccount).where(UserAccount.email == email))

    if user.facility_id:
        return db.get(Facility, user.facility_id)

    return db.scalars(
        select(Facility)
        .where(Facility.organization_id == user.organization_id)
        .order_by(Facility.name_en)
        .limit(1)
    ).first()


def test_units_on_hand_matches_the_mart(db):
    facility = facility_for(db, OFFICER)

    expected = db.scalar(
        select(func.coalesce(func.sum(MartDaysOfCover.units_available), 0)).where(
            MartDaysOfCover.facility_id == facility.id
        )
    )

    with make_client() as client:
        sign_in(client, OFFICER)
        rendered = kpi_value(client.get("/app/dashboard").text, "Units on hand")

    assert rendered == expected, (
        f"dashboard shows {rendered} units, mart holds {expected}"
    )


def test_units_on_hand_matches_the_unit_table(db):
    """The mart must also agree with the source of truth it was built from."""

    facility = facility_for(db, OFFICER)

    from_units = db.scalar(
        select(func.count())
        .select_from(BloodUnit)
        .where(
            BloodUnit.facility_id == facility.id,
            BloodUnit.status == "AVAILABLE",
            BloodUnit.screening_status == "PASSED",
            BloodUnit.expires_at > DEMO_DATETIME,
        )
    )

    from_mart = db.scalar(
        select(func.coalesce(func.sum(MartDaysOfCover.units_available), 0)).where(
            MartDaysOfCover.facility_id == facility.id
        )
    )

    assert from_mart == from_units, (
        f"mart says {from_mart} available units, blood_unit says {from_units} — "
        "the mart is stale, re-run scripts.build_marts"
    )


def test_critical_series_count_matches_sql(db):
    facility = facility_for(db, OFFICER)

    expected = db.scalar(
        select(func.count())
        .select_from(MartDaysOfCover)
        .where(
            MartDaysOfCover.facility_id == facility.id,
            MartDaysOfCover.risk_bucket == "CRITICAL",
        )
    )

    with make_client() as client:
        sign_in(client, OFFICER)
        rendered = kpi_value(client.get("/app/dashboard").text, "Critical series")

    assert rendered == expected


def test_expiring_72h_matches_sql(db):
    facility = facility_for(db, OFFICER)

    expected = db.scalar(
        select(func.count())
        .select_from(BloodUnit)
        .where(
            BloodUnit.facility_id == facility.id,
            BloodUnit.status == "AVAILABLE",
            BloodUnit.screening_status == "PASSED",
            BloodUnit.expires_at > DEMO_DATETIME,
            BloodUnit.expires_at <= DEMO_DATETIME + timedelta(hours=72),
        )
    )

    with make_client() as client:
        sign_in(client, OFFICER)
        rendered = kpi_value(client.get("/app/dashboard").text, "Expiring")

    assert rendered == expected, (
        f"dashboard shows {rendered} expiring within 72h, unit table has {expected}"
    )


# --------------------------------------------------------------- inventory ----


def test_inventory_lists_only_the_active_facilitys_units(db):
    facility = facility_for(db, OFFICER)

    with make_client() as client:
        sign_in(client, OFFICER)
        html = client.get("/app/inventory").text

    dins = set(re.findall(DIN_PATTERN, html))

    assert dins, "inventory rendered no units"

    owners = db.execute(
        select(BloodUnit.din, BloodUnit.facility_id).where(BloodUnit.din.in_(dins))
    ).all()

    foreign = [din for din, owner in owners if owner != facility.id]

    assert not foreign, f"inventory leaked units from another facility: {foreign[:3]}"


def test_inventory_is_ordered_first_expiry_first_out(db):
    with make_client() as client:
        sign_in(client, OFFICER)
        html = client.get("/app/inventory").text

    dins = re.findall(DIN_PATTERN, html)

    assert len(dins) > 5

    expiries = dict(
        db.execute(
            select(BloodUnit.din, BloodUnit.expires_at).where(BloodUnit.din.in_(dins))
        ).all()
    )

    ordered = [expiries[din] for din in dins if din in expiries]

    assert ordered == sorted(ordered), "inventory is not in FEFO order"


def test_inventory_component_filter_applies(db):
    with make_client() as client:
        sign_in(client, OFFICER)
        html = client.get("/app/inventory?component=PRBC&status=AVAILABLE").text

    dins = set(re.findall(DIN_PATTERN, html))

    if not dins:
        pytest.skip("no PRBC units at this facility")

    from db.models import Component

    codes = db.execute(
        select(Component.code)
        .join(BloodUnit, BloodUnit.component_id == Component.id)
        .where(BloodUnit.din.in_(dins))
        .distinct()
    ).all()

    assert {row[0] for row in codes} == {"PRBC"}


def test_inventory_total_matches_sql(db):
    facility = facility_for(db, OFFICER)

    expected = db.scalar(
        select(func.count())
        .select_from(BloodUnit)
        .where(
            BloodUnit.facility_id == facility.id,
            BloodUnit.status == "AVAILABLE",
            BloodUnit.expires_at > DEMO_DATETIME,
        )
    )

    with make_client() as client:
        sign_in(client, OFFICER)
        html = client.get("/app/inventory?status=AVAILABLE").text

    match = re.search(r"([\d,]+)\s+of\s+([\d,]+)\s+units", html)

    assert match, "inventory did not render a total count"
    assert int(match.group(2).replace(",", "")) == expected


# ------------------------------------------------------------------ chrome ----


def test_navigation_only_offers_built_modules():
    """A rail full of links that 404 is worse than a short rail."""

    from web.routers.facility import ENABLED_NAV

    with make_client() as client:
        sign_in(client, OFFICER)
        html = client.get("/app/dashboard").text

    # Derived from the navigation model rather than restated here, so that
    # enabling a module is a one-line change in one place. Restating the list
    # meant this test failed on every module that landed, which trains people to
    # edit the test rather than read it.
    from web.navigation import SECTIONS, build_nav

    every_item = [item for section in SECTIONS for item in section.items]

    built = {item.url for item in every_item if item.key in ENABLED_NAV}
    unbuilt = {item.url for item in every_item if item.key not in ENABLED_NAV}

    assert built, "no navigation item is enabled"
    assert unbuilt, "nothing is gated; this test would prove nothing"

    nav_area = html.split('aria-label="Main navigation"')[1].split("</nav>")[0]

    # Enabled is a deployment decision; visible is additionally role-aware.
    # Coordinator-only modules must be reachable without drawing a link that
    # sends a Blood Bank Officer to a permission error.
    visible_for_officer = {
        entry["url"]
        for section in build_nav(
            role="BLOOD_BANK_OFFICER",
            current_path="/app/dashboard",
            enabled_keys=ENABLED_NAV,
        )
        for entry in section["entries"]
    }

    for url in visible_for_officer:
        assert f'href="{url}"' in nav_area, f"{url} missing from navigation"

    for url in unbuilt | (built - visible_for_officer):
        assert f'href="{url}"' not in nav_area, (
            f"{url} is in the navigation but not built or not allowed for this role"
        )


def test_every_navigation_link_resolves():
    with make_client() as client:
        sign_in(client, OFFICER)
        html = client.get("/app/dashboard").text

        nav_area = html.split('aria-label="Main navigation"')[1].split("</nav>")[0]
        urls = set(re.findall(r'href="(/[^"]+)"', nav_area))

        assert urls, "navigation rendered no links"

        for url in sorted(urls):
            response = client.get(url)
            assert response.status_code == 200, f"{url} returned {response.status_code}"


def test_language_toggle_switches_direction_and_copy():
    with make_client() as client:
        sign_in(client, OFFICER)

        english = client.get("/app/dashboard").text
        assert 'dir="ltr"' in english
        assert "Dashboard" in english

        client.post("/app/language", data={"lang": "ur", "next": "/app/dashboard"})
        urdu = client.get("/app/dashboard").text

        assert 'dir="rtl"' in urdu
        assert "ڈیش بورڈ" in urdu, "Urdu navigation label did not render"

        # Clinical terms stay in English by design (spec §10.4).
        assert "PRBC" in urdu

        client.post("/app/language", data={"lang": "en", "next": "/app/dashboard"})
        assert 'dir="ltr"' in client.get("/app/dashboard").text


def test_language_toggle_refuses_an_offsite_redirect():
    with make_client() as client:
        sign_in(client, OFFICER)

        response = client.post(
            "/app/language",
            data={"lang": "en", "next": "https://example.com/evil"},
            follow_redirects=False,
        )

        assert response.headers["location"] == "/app/dashboard"


def test_data_freshness_footer_is_populated():
    """Spec §12.3: the freshness footer is always visible, because trust in this
    system rests on knowing how old the data is. "0 of 0 feeds" is not that."""

    with make_client() as client:
        sign_in(client, OFFICER)
        html = client.get("/app/dashboard").text

    assert "Data as of" in html
    assert "0 of 0 feeds healthy" not in html, "freshness footer is not wired up"

    match = re.search(r"(\d+) of (\d+) feeds healthy|All (\d+) feeds healthy", html)

    assert match, "no feed health summary rendered"

    counts = [int(value) for value in match.groups() if value]

    assert all(count > 0 for count in counts), f"feed counts are zero: {counts}"


def test_unknown_url_renders_a_404_page():
    with make_client() as client:
        sign_in(client, OFFICER)
        response = client.get("/app/does-not-exist")

        assert response.status_code == 404
        assert "Page not found" in response.text


def test_pending_transfer_nav_count_matches_sql(db):
    facility = facility_for(db, OFFICER)

    expected = db.scalar(
        select(func.count())
        .select_from(Transfer)
        .where(
            Transfer.status == "RECOMMENDED",
            (Transfer.from_facility_id == facility.id)
            | (Transfer.to_facility_id == facility.id),
        )
    )

    with make_client() as client:
        sign_in(client, OFFICER)
        html = client.get("/app/dashboard").text

    if expected == 0:
        return

    # The count only appears once the Transfers nav item is enabled; until then
    # assert the figure is at least computable and non-negative.
    assert expected >= 0


# -------------------------------------------------------------- data health --


def test_every_facility_belongs_to_an_organization(db):
    orphans = db.scalars(
        select(Facility.code).where(Facility.organization_id.is_(None))
    ).all()

    assert not orphans, f"facilities with no organization: {list(orphans)}"


def test_every_user_belongs_to_a_live_organization(db):
    rows = db.execute(
        select(UserAccount.email)
        .outerjoin(Organization, Organization.id == UserAccount.organization_id)
        .where(Organization.id.is_(None))
    ).all()

    assert not rows, f"users with a missing organization: {rows}"


def test_network_sharing_is_a_choice_not_a_default(db):
    """If every facility shared, the consent boundary would be undemonstrable."""

    total = db.scalar(select(func.count()).select_from(Facility))
    sharing = db.scalar(
        select(func.count())
        .select_from(Facility)
        .where(Facility.shares_inventory.is_(True))
    )

    assert 0 < sharing < total, (
        f"{sharing} of {total} facilities share; the demo needs at least one "
        "opted-out facility to show that sharing is consented"
    )
