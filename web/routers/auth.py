"""Sign in, sign out, facility switching and language.

Failed logins are counted and the account locks, per spec §13.2's rate-limiting
requirement. The response to a bad email and a bad password is identical, so the
form cannot be used to discover which accounts exist.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Scope
from db.models import AuditLog, Facility, Organization, UserAccount, UserSession, new_id
from config.settings import SESSION_COOKIE_SAMESITE, SESSION_COOKIE_SECURE
from i18n.t import t
from web import security
from web.deps import Principal, get_db, optional_principal, require_principal
from web.templating import current_lang, flash, render, set_lang

router = APIRouter()

DEMO_PASSWORD = "Rabta@2026"

# The sign-in page can list every seeded account and the shared password, which
# makes the tenancy model demonstrable — you cannot see that two organizations
# are isolated without signing in as both. It is also a complete credential dump
# on an unauthenticated page, in the same module that goes to some trouble to
# make login responses indistinguishable so the form cannot be used to discover
# which accounts exist.
#
# So it is opt-in, and absent unless someone deliberately turns it on:
#
#     RABTA_SHOW_DEMO_LOGINS=1 python -m uvicorn web.main:app
#
# Anything demo-only that follows should use the same switch rather than
# inventing another one.
SHOW_DEMO_LOGINS = os.environ.get("RABTA_SHOW_DEMO_LOGINS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _demo_accounts(db: Session) -> list[dict]:
    """Seeded accounts for the sign-in page, when explicitly enabled.

    Returns nothing unless SHOW_DEMO_LOGINS is set, so the default deployment
    publishes no account list at all.
    """

    if not SHOW_DEMO_LOGINS:
        return []

    rows = db.execute(
        select(
            UserAccount.email,
            UserAccount.full_name,
            UserAccount.role,
            Organization.name_en,
            Organization.org_type,
        )
        .join(Organization, Organization.id == UserAccount.organization_id)
        .where(UserAccount.is_active.is_(True))
        .order_by(Organization.name_en, UserAccount.email)
    ).all()

    return [
        {
            "email": row[0],
            "full_name": row[1],
            "role": row[2],
            "organization": row[3],
            "org_type": row[4],
        }
        for row in rows
    ]


# Where a redirect is allowed to send someone. Anything not matching goes to the
# dashboard instead of off-site.
SAFE_REDIRECT_DEFAULT = "/app/dashboard"


def safe_redirect(target: str | None, default: str = SAFE_REDIRECT_DEFAULT) -> str:
    """A same-origin path, or the default.

    `target.startswith("/")` is not sufficient, and that was the bug:
    "//evil.example" starts with a slash and is a protocol-relative URL, so a
    browser reads it as an absolute address on another host. "/\\evil.example"
    is normalised to the same thing by several browsers. Control characters can
    smuggle a slash past a prefix test and can split a response header.

    One shape is allowed: a single leading slash, the next character neither a
    slash nor a backslash, and no scheme in the path.
    """

    if not target:
        return default

    candidate = "".join(ch for ch in str(target).strip() if ch.isprintable())

    if not candidate.startswith("/"):
        return default

    if candidate[1:2] in ("/", "\\"):
        return default

    # A scheme in the path portion, e.g. "/https://evil.example". The query
    # string may legitimately contain a colon and is not the redirect target.
    path = candidate.split("?", 1)[0].split("#", 1)[0]

    if ":" in path:
        return default

    return candidate


@router.get("/login")
def login_form(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal | None = Depends(optional_principal),
):
    if principal is not None:
        destination = "/account/password" if principal.user.must_change_password else "/app/dashboard"
        return RedirectResponse(destination, status_code=303)

    return render(
        request,
        "auth/login.html",
        {
            "demo_accounts": _demo_accounts(db),
            "demo_password": DEMO_PASSWORD if SHOW_DEMO_LOGINS else None,
        },
        page_title=t("auth.sign_in", language=current_lang(request)),
    )


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    lang = current_lang(request)
    normalised = (email or "").strip().lower()

    user = db.scalar(select(UserAccount).where(UserAccount.email == normalised))

    def reject(message_key: str, **params):
        flash(request, t(message_key, language=lang, **params), "critical")

        return RedirectResponse("/login", status_code=303)

    if user is None:
        # Same response as a wrong password, so the form does not enumerate users.
        return reject("auth.invalid")

    if security.is_locked(user):
        return reject("auth.locked", minutes=security.LOCKOUT_MINUTES)

    if not user.is_active:
        return reject("auth.inactive")

    if not security.verify_password(password, user.password_hash):
        user.failed_login_count = (user.failed_login_count or 0) + 1

        if user.failed_login_count >= security.MAX_FAILED_LOGINS:
            user.locked_until = security.lockout_until()
            user.failed_login_count = 0

        db.commit()

        return reject("auth.invalid")

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = security.now()

    home_facility = user.facility_id

    if home_facility is None:
        home_facility = db.scalar(
            select(Facility.id)
            .where(
                Facility.organization_id == user.organization_id,
                Facility.is_active.is_(True),
            )
            .order_by(Facility.name_en)
            .limit(1)
        )

    token = security.new_session_token()

    db.add(
        UserSession(
            id=token,
            user_id=user.id,
            active_facility_id=home_facility,
            created_at=security.now(),
            last_seen_at=security.now(),
            expires_at=security.session_expiry(),
            ip_address=request.client.host if request.client else None,
            user_agent=(request.headers.get("user-agent") or "")[:255],
        )
    )

    db.add(
        AuditLog(
            id=new_id(),
            created_at=security.now(),
            actor=user.email,
            action="auth.login",
            entity_type="user_account",
            entity_id=user.id,
            after_json={"organization_id": user.organization_id},
            actor_ip=request.client.host if request.client else None,
        )
    )

    db.commit()

    destination = "/account/password" if user.must_change_password else "/app/dashboard"
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        security.SESSION_COOKIE,
        token,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite=SESSION_COOKIE_SAMESITE,
        max_age=security.SESSION_ABSOLUTE_HOURS * 3600,
        path="/",
    )

    return response


def _password_page(request: Request, db: Session, principal: Principal):
    from web.routers.facility import ENABLED_NAV, nav_counts

    return render(
        request,
        "auth/password.html",
        {"forced": bool(principal.user.must_change_password)},
        principal=principal,
        db=db,
        nav_counts=nav_counts(db, principal),
        enabled_nav=ENABLED_NAV,
        page_title=t("auth.change_password", language=current_lang(request)),
    )


@router.get("/account/password")
def password_form(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal | None = Depends(optional_principal),
):
    if principal is None:
        return RedirectResponse("/login", status_code=303)
    return _password_page(request, db, principal)


@router.post("/account/password")
def password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal | None = Depends(optional_principal),
):
    if principal is None:
        return RedirectResponse("/login", status_code=303)
    lang = current_lang(request)
    if not security.verify_password(current_password, principal.user.password_hash):
        flash(request, t("auth.current_password_invalid", language=lang), "critical")
        return RedirectResponse("/account/password", status_code=303)
    if new_password != confirm_password:
        flash(request, t("auth.passwords_do_not_match", language=lang), "critical")
        return RedirectResponse("/account/password", status_code=303)
    if security.verify_password(new_password, principal.user.password_hash):
        flash(request, t("auth.password_must_change", language=lang), "critical")
        return RedirectResponse("/account/password", status_code=303)
    from services.audit import Actor, ServiceError, audited
    from services.network_onboarding import validate_temporary_password

    try:
        validate_temporary_password(new_password)
    except ServiceError:
        flash(request, t("auth.password_requirements", language=lang), "critical")
        return RedirectResponse("/account/password", status_code=303)

    other_sessions = list(
        db.scalars(
            select(UserSession).where(
                UserSession.user_id == principal.user.id,
                UserSession.id != principal.session.id,
                UserSession.revoked_at.is_(None),
            )
        ).all()
    )
    with audited(
        db,
        Actor.from_principal(principal, request),
        "user.password.change",
        "user_account",
        principal.user.id,
    ) as entry:
        was_forced = bool(principal.user.must_change_password)
        principal.user.password_hash = security.hash_password(new_password)
        principal.user.must_change_password = False
        for session in other_sessions:
            session.revoked_at = security.now()
        entry.on(
            principal.user,
            before={"must_change_password": was_forced},
            after={
                "must_change_password": False,
                "other_sessions_revoked": len(other_sessions),
            },
        )
    flash(request, t("auth.password_changed", language=lang), "safe")
    return RedirectResponse("/app/dashboard", status_code=303)


@router.post("/logout")
def logout(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal | None = Depends(optional_principal),
):
    lang = current_lang(request)

    if principal is not None:
        session = db.get(UserSession, principal.session.id)

        if session is not None:
            session.revoked_at = security.now()

        db.add(
            AuditLog(
                id=new_id(),
                created_at=security.now(),
                actor=principal.user.email,
                action="auth.logout",
                entity_type="user_account",
                entity_id=principal.user_id,
            )
        )
        db.commit()

    flash(request, t("auth.signed_out", language=lang), "info")

    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(
        security.SESSION_COOKIE,
        path="/",
        secure=SESSION_COOKIE_SECURE,
        httponly=True,
        samesite=SESSION_COOKIE_SAMESITE,
    )

    return response


@router.post("/app/switch-facility")
def switch_facility(
    request: Request,
    facility_id: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    """Move between the organization's own facilities without re-authenticating.

    The guard is the point: a facility id arriving in a form body is untrusted,
    and switching to another tenant's blood bank must be impossible.
    """

    principal.require_own_facility(facility_id)

    # A facility-pinned user belongs to one blood bank. Being in a hospital
    # group does not silently grant permission to work in every sibling
    # facility; only a group-level account may change the active facility.
    if not principal.is_group_user and facility_id != principal.user.facility_id:
        raise HTTPException(status_code=404, detail="Facility is outside this account scope")

    session = db.get(UserSession, principal.session.id)

    if session is not None:
        session.active_facility_id = facility_id
        db.commit()

    facility = db.get(Facility, facility_id)

    flash(
        request,
        t(
            "auth.facility_switched",
            language=current_lang(request),
            facility=facility.name_en if facility else "",
        ),
        "info",
    )

    # The Referer header is attacker-controllable, so it is validated rather
    # than trusted. Redirecting to a raw Referer is an open redirect that a
    # phishing page can drive by linking here.
    return RedirectResponse(
        safe_redirect(request.headers.get("referer")), status_code=303
    )


@router.post("/app/language")
def change_language(
    request: Request,
    lang: str = Form(...),
    next: str = Form("/app/dashboard"),
):
    set_lang(request, lang)

    return RedirectResponse(safe_redirect(next), status_code=303)


@router.post("/app/switch-scope")
def switch_scope(
    request: Request,
    scope: str = Form(...),
    next: str = Form("/app/dashboard"),
    principal: Principal = Depends(require_principal),
):
    """Change the aggregate intelligence scope within the role's ceiling."""

    try:
        selected = Scope(scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Unknown network scope") from exc

    if selected not in principal.selectable_scopes:
        raise HTTPException(
            status_code=403,
            detail="Your role does not permit this network scope",
        )

    request.session["network_scope"] = selected.value
    return RedirectResponse(safe_redirect(next), status_code=303)
