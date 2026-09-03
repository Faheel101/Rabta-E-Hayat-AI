"""Jinja environment, shared template context and flash messages."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.auth import ORG_TYPE_LABEL_KEYS, ROLE_LABEL_KEYS, SCOPE_LABEL_KEYS, Role
from config.settings import APP_VERSION, DATA_NOTICE, SYNTHETIC_DATA
from i18n.t import LANGUAGES, direction, t
from core.clock import DEMO_DATETIME, days_until, hours_until
from web import navigation
from services.onboarding_service import state as onboarding_state

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

FLASH_KEY = "_flashes"
LANG_KEY = "lang"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ------------------------------------------------------------------ filters ---


def _fmt_num(value, decimals: int = 0) -> str:
    if value is None:
        return "—"

    try:
        if decimals:
            return f"{float(value):,.{decimals}f}"

        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct(value, decimals: int = 0) -> str:
    return "—" if value is None else f"{float(value):.{decimals}f}%"


def _fmt_money(value) -> str:
    if value is None:
        return "—"

    amount = float(value)

    if amount >= 1e7:
        return f"Rs {amount / 1e7:.2f} crore"

    if amount >= 1e5:
        return f"Rs {amount / 1e5:.2f} lakh"

    return f"Rs {amount:,.0f}"


def _fmt_date(value, pattern: str = "%d %b %Y") -> str:
    if value is None:
        return "—"

    if isinstance(value, str):
        return value

    return value.strftime(pattern)


def _fmt_datetime(value, pattern: str = "%d %b %Y, %H:%M") -> str:
    return _fmt_date(value, pattern)


def _fmt_age_hours(hours) -> str:
    if hours is None:
        return "—"

    hours = float(hours)

    if hours < 0:
        return "elapsed"

    if hours < 1:
        return f"{int(hours * 60)} min"

    if hours < 48:
        return f"{int(hours)}h {int((hours % 1) * 60):02d}m"

    return f"{hours / 24:.1f} d"


GROUP_CLASS = {
    "O+": "bg-group-o-pos",
    "O-": "bg-group-o-neg",
    "A+": "bg-group-a-pos",
    "A-": "bg-group-a-neg",
    "B+": "bg-group-b-pos",
    "B-": "bg-group-b-neg",
    "AB+": "bg-group-ab-pos",
    "AB-": "bg-group-ab-neg",
}


def _group_class(code: str) -> str:
    return GROUP_CLASS.get(code, "bg-ink-muted")


templates.env.filters["num"] = _fmt_num
templates.env.filters["pct"] = _fmt_pct
templates.env.filters["money"] = _fmt_money
templates.env.filters["date"] = _fmt_date
templates.env.filters["datetime"] = _fmt_datetime
templates.env.filters["age_hours"] = _fmt_age_hours
templates.env.filters["group_class"] = _group_class

# Datetime arithmetic belongs here, not in a template: SQLite returns naive
# datetimes and the demo instant is aware, so subtracting them in Jinja raises.
templates.env.filters["hours_until"] = hours_until
templates.env.filters["days_until"] = days_until
templates.env.filters["tojson_safe"] = lambda value: json.dumps(value)

templates.env.globals["t"] = t
templates.env.globals["languages"] = LANGUAGES


# ------------------------------------------------------------------ flashes ---


def flash(request: Request, message: str, tone: str = "info") -> None:
    """Queue a one-shot message for the next rendered page.

    Held in the signed session cookie, not the database: a message is
    presentational and should not outlive the redirect that carries it.
    """

    queue = request.session.setdefault(FLASH_KEY, [])
    queue.append({"message": message, "tone": tone})


def consume_flashes(request: Request) -> list[dict]:
    return request.session.pop(FLASH_KEY, [])


def current_lang(request: Request) -> str:
    lang = request.session.get(LANG_KEY, "en")

    return lang if lang in LANGUAGES else "en"


def set_lang(request: Request, lang: str) -> None:
    if lang in LANGUAGES:
        request.session[LANG_KEY] = lang


# ------------------------------------------------------------------- feeds ----


def feed_health(db, principal) -> dict:
    """Feed freshness across the organization's own facilities.

    Spec §12.3 keeps this footer permanently visible because trust in the system
    rests on the user knowing exactly how old the data is, and §5.8's degradation
    principle says a facility with a stale feed is visibly marked rather than
    silently dropped. A footer reading "0 of 0 feeds healthy" satisfies neither.
    """

    from services.feed_health_service import snapshot

    return snapshot(db, principal.scope_facilities)


# ------------------------------------------------------------------ render ----


def render(
    request: Request,
    template: str,
    context: dict | None = None,
    *,
    principal=None,
    page_title: str = "",
    breadcrumbs: list[dict] | None = None,
    nav_counts: dict | None = None,
    enabled_nav: set[str] | None = None,
    db=None,
    status_code: int = 200,
):
    """Render a page with the shared chrome context already populated."""

    lang = current_lang(request)

    # t() reads the language from Streamlit session state when inside Streamlit;
    # here the request carries it, so bind it explicitly for this render.
    def translate(key: str, **params):
        return t(key, language=lang, **params)

    role_label = ""
    org_type_label = ""

    if principal is not None:
        try:
            role_label = translate(ROLE_LABEL_KEYS[Role(principal.role)])
        except (ValueError, KeyError):
            role_label = principal.role

        org_type_label = translate(
            ORG_TYPE_LABEL_KEYS.get(
                principal.organization.org_type, "org_type.standalone"
            )
        )

    base = {
        "request": request,
        "principal": principal,
        "lang": lang,
        "direction": direction(lang),
        "t": translate,
        "page_title": page_title,
        "breadcrumbs": breadcrumbs or [],
        "flashes": consume_flashes(request),
        "role_label": role_label,
        "org_type_label": org_type_label,
        "data_as_of": DEMO_DATETIME.strftime("%d %b %Y, %H:%M"),
        "data_notice": DATA_NOTICE,
        "synthetic_data": SYNTHETIC_DATA,
        "app_version": APP_VERSION,
        "feeds_healthy": 0,
        "feeds_total": 0,
        "intelligence_refresh": {
            "status": "UNINITIALIZED",
            "pending": True,
            "completed_at": None,
            "duration_ms": None,
            "last_error": None,
        },
        "nav": [],
        "network_scopes": [],
        "onboarding": {"version": 1, "complete": True, "completed_at": None},
    }

    from services.ai_service import runtime_status as ai_runtime_status

    base["ai_runtime"] = ai_runtime_status()

    if principal is not None:
        base["onboarding"] = onboarding_state(principal.user)
        base["nav"] = navigation.build_nav(
            role=principal.role,
            current_path=request.url.path,
            counts=nav_counts,
            enabled_keys=enabled_nav,
            language=current_lang(request),
        )
        base["network_scopes"] = [
            {
                "value": scope.value,
                "label": translate(SCOPE_LABEL_KEYS[scope]),
                "selected": scope == principal.selected_scope,
            }
            for scope in principal.selectable_scopes
        ]
        if db is not None:
            base.update(feed_health(db, principal))
            from services.intelligence_refresh import status_snapshot

            base["intelligence_refresh"] = status_snapshot(db)

    base.update(context or {})

    return templates.TemplateResponse(
        request, template, base, status_code=status_code
    )
