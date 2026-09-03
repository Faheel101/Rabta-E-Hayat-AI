"""Redirect safety, credential exposure, and the language actually being used.

All three were audit findings in the same layer. The first two are security; the
third is the kind of bug that looks like missing translation work and is really a
forgotten argument.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from starlette.testclient import TestClient

from web.main import app
from web.routers.auth import safe_redirect

PASSWORD = "Rabta@2026"
OFFICER = "dr.ahmed@punjab-teaching.rabta.pk"

REPO = pathlib.Path(__file__).resolve().parents[2]


def make_client() -> TestClient:
    return TestClient(app, follow_redirects=True)


def sign_in(client: TestClient):
    return client.post("/login", data={"email": OFFICER, "password": PASSWORD})


# -------------------------------------------------------------- open redirect


@pytest.mark.parametrize(
    "hostile",
    [
        "//evil.example/phish",
        "/\\evil.example",
        "https://evil.example",
        "http://evil.example",
        "javascript:alert(1)",
        "/https://evil.example",
        "app/relative",
        "\t//evil.example",
    ],
)
def test_a_hostile_redirect_target_is_refused(hostile):
    """`startswith("/")` was the whole check, and it is not enough:
    "//evil.example" starts with a slash and is a protocol-relative URL that
    sends the browser to another host."""

    assert safe_redirect(hostile) == "/app/dashboard"


@pytest.mark.parametrize(
    "benign",
    ["/app/donors", "/app/donors?q=Khan", "/app/inventory?status=AVAILABLE", "/"],
)
def test_a_same_origin_path_is_preserved(benign):
    assert safe_redirect(benign) == benign


def test_a_redirect_target_cannot_smuggle_a_response_header():
    assert "\r" not in safe_redirect("/app/x\r\nSet-Cookie: admin=1")
    assert "\n" not in safe_redirect("/app/x\r\nSet-Cookie: admin=1")


def test_the_language_form_will_not_redirect_off_site():
    with make_client() as client:
        sign_in(client)
        response = client.post(
            "/app/language",
            data={"lang": "en", "next": "//evil.example/phish"},
            follow_redirects=False,
        )

    assert "evil.example" not in response.headers.get("location", "")


def test_switching_facility_does_not_trust_the_referer_header():
    """The Referer is attacker-controllable — a phishing page can set it by
    linking here."""

    with make_client() as client:
        sign_in(client)
        response = client.post(
            "/app/switch-facility",
            data={"facility_id": "nonexistent"},
            headers={"referer": "https://evil.example/phish"},
            follow_redirects=False,
        )

    assert "evil.example" not in response.headers.get("location", "")


# ------------------------------------------------------- credential exposure


def test_the_sign_in_page_publishes_no_credentials_by_default():
    """The account list and shared password are a credential dump on an
    unauthenticated page, in the same module that works to make login responses
    indistinguishable so accounts cannot be enumerated."""

    with make_client() as client:
        body = client.get("/login").text

    assert PASSWORD not in body, "the shared password is printed on the sign-in page"
    assert "rabta.pk" not in body, "seeded account emails are listed on the sign-in page"


def test_the_demo_listing_is_opt_in_by_environment(monkeypatch):
    import importlib

    import web.routers.auth as auth

    monkeypatch.setenv("RABTA_SHOW_DEMO_LOGINS", "1")
    importlib.reload(auth)

    assert auth.SHOW_DEMO_LOGINS is True

    monkeypatch.delenv("RABTA_SHOW_DEMO_LOGINS")
    importlib.reload(auth)

    assert auth.SHOW_DEMO_LOGINS is False


def test_the_demo_listing_names_bench_roles_truthfully(monkeypatch):
    import importlib

    import web.routers.auth as auth

    monkeypatch.setenv("RABTA_SHOW_DEMO_LOGINS", "1")
    importlib.reload(auth)

    try:
        with make_client() as client:
            body = client.get("/login").text

        assert "Nasreen Bibi · Phlebotomist" in body
        assert "Rizwan Aslam · Lab Technologist" in body
    finally:
        monkeypatch.delenv("RABTA_SHOW_DEMO_LOGINS")
        importlib.reload(auth)


# ------------------------------------------------------------------- language


def urdu_chars(text: str) -> int:
    return sum(1 for ch in text if "؀" <= ch <= "ۿ")


def test_the_navigation_renders_in_urdu():
    """build_nav called t() with no language, so the entire sidebar came back in
    English while ur.json held every translation."""

    with make_client() as client:
        sign_in(client)
        english = client.get("/app/dashboard").text
        client.post("/app/language", data={"lang": "ur", "next": "/app/dashboard"})
        urdu = client.get("/app/dashboard").text

    def nav(html: str, label: str) -> str:
        return html.split(f'aria-label="{label}"')[1].split("</nav>")[0]

    assert urdu_chars(nav(english, "Main navigation")) == 0
    assert urdu_chars(nav(urdu, "مرکزی سمت شناسی")) > 0, (
        "the navigation rail is still English under ur"
    )
    assert "Donor Register" not in nav(urdu, "مرکزی سمت شناسی")


def test_error_pages_render_in_urdu():
    with make_client() as client:
        sign_in(client)
        client.post("/app/language", data={"lang": "ur", "next": "/app/dashboard"})
        response = client.get("/app/definitely-not-a-page")

    assert response.status_code == 404
    assert urdu_chars(response.text) > 0


def test_rtl_direction_is_set_for_urdu():
    with make_client() as client:
        sign_in(client)
        client.post("/app/language", data={"lang": "ur", "next": "/app/dashboard"})
        body = client.get("/app/dashboard").text

    assert 'dir="rtl"' in body


def test_mixed_direction_scenario_timestamp_is_isolated_in_urdu():
    with make_client() as client:
        sign_in(client)
        client.post("/app/language", data={"lang": "ur", "next": "/app/dashboard"})
        body = client.get("/app/dashboard").text

    assert '<bdi class="num" dir="ltr">06 Aug 2026, 08:00</bdi>' in body
    assert "کے مطابق مقررہ اور قابلِ تکرار منظرنامہ" in body


# ---------------------------------------------------- translation bookkeeping


def _leaves(node: dict, prefix: str = "") -> dict:
    out = {}

    for key, value in node.items():
        if key == "_meta":
            continue

        path = f"{prefix}{key}"

        if isinstance(value, dict):
            out.update(_leaves(value, path + "."))
        else:
            out[path] = value

    return out


def test_every_english_key_has_an_urdu_entry():
    """The whole enabled product is bilingual; only clinical component codes
    deliberately fall back to English under spec section 10.4."""

    english = _leaves(json.loads((REPO / "i18n" / "en.json").read_text(encoding="utf-8")))
    urdu = _leaves(json.loads((REPO / "i18n" / "ur.json").read_text(encoding="utf-8")))

    missing = sorted(
        key
        for key in set(english) - set(urdu)
        if not key.startswith("components.")
    )

    assert not missing, f"English keys with no Urdu entry: {missing}"


def test_clinical_component_names_are_deliberately_untranslated():
    """Spec §10.4: a Pakistani blood bank officer says 'platelets'. This asserts
    the omission is intentional rather than an oversight somebody later 'fixes'
    by machine-translating transfusion terminology."""

    urdu = json.loads((REPO / "i18n" / "ur.json").read_text(encoding="utf-8"))

    assert "components" not in urdu or not urdu["components"]


def test_the_translation_file_states_its_own_coverage():
    """The note used to imply fuller coverage than existed. It must say which
    surfaces are done and which are not."""

    urdu = json.loads((REPO / "i18n" / "ur.json").read_text(encoding="utf-8"))
    meta = urdu.get("_meta", {})

    assert meta.get("covered_surfaces"), "_meta does not say what is covered"
    assert meta.get("not_covered"), "_meta does not say what is missing"
    assert meta.get("status") == "AWAITING_NATIVE_SPEAKER_REVIEW"


# --------------------------------------------------------- Sprint 6 quality


def test_runtime_templates_do_not_depend_on_remote_assets():
    templates = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO / "web" / "templates").rglob("*.html")
    ).lower()

    forbidden = (
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "unpkg.com",
        "cdn.jsdelivr.net",
    )

    assert not [host for host in forbidden if host in templates]


def test_application_shell_exposes_keyboard_and_live_region_landmarks():
    base = (REPO / "web" / "templates" / "layout" / "base.html").read_text(
        encoding="utf-8"
    )

    assert 'class="skip-link"' in base
    assert 'href="#main"' in base
    assert '<main ' in base and 'id="main"' in base and 'tabindex="-1"' in base
    assert 'aria-live="polite"' in base
    assert "@keydown.escape" in base


def test_templates_do_not_use_retired_semantic_colour_names():
    templates = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO / "web" / "templates").rglob("*.html")
    )

    retired = (
        "badge-success",
        "badge-warning",
        "text-success",
        "text-warning",
        "bg-success",
        "bg-warning",
    )

    assert not [name for name in retired if name in templates]


@pytest.mark.parametrize(
    "path",
    (
        "/app/dashboard",
        "/app/sessions",
        "/app/donors",
        "/app/signoff",
        "/app/lab",
        "/app/processing",
        "/app/inventory",
        "/app/inventory/storage",
    ),
)
def test_operational_pages_render_complete_urdu_without_missing_keys(path):
    with make_client() as client:
        sign_in(client)
        client.post("/app/language", data={"lang": "ur", "next": path})
        response = client.get(path)

    assert response.status_code == 200
    assert 'dir="rtl"' in response.text
    assert urdu_chars(response.text) > 100
    assert "[ops." not in response.text
    assert "[eligibility." not in response.text
    assert "[status." not in response.text
