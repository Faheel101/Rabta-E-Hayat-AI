"""FastAPI application.

    .venv/Scripts/python -m uvicorn web.main:app --reload

Server-rendered Jinja with HTMX for partial updates. The choice is deliberate:
this is a data-entry system before it is a dashboard, and a donor registration
form or a crossmatch entry wants server-side validation and a real URL, not a
client-side store.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from config.settings import (
    APP_NAME,
    APP_VERSION,
    AUTO_CREATE_SCHEMA,
    DATA_NOTICE,
    DEMO_DATE,
    FORCE_HTTPS,
    INTELLIGENCE_REFRESH_ENABLED,
    INTELLIGENCE_REFRESH_POLL_SECONDS,
    LOG_JSON,
    LOG_LEVEL,
    SECRET_KEY,
    SESSION_COOKIE_SAMESITE,
    SESSION_COOKIE_SECURE,
    SYNTHETIC_DATA,
    TRUSTED_HOSTS,
    validate_runtime_config,
)
from db.readiness import readiness_report
from db.session import SessionLocal, init_db
from services import request_service
from services.audit import Actor
from web import security
from web.deps import optional_principal
from web.routers import auth as auth_router
from web.routers import collection as collection_router
from web.routers import donors as donors_router
from web.routers import facility as facility_router
from web.routers import inventory as inventory_router
from web.routers import insights as insights_router
from web.routers import emergency as emergency_router
from web.routers import alerts as alerts_router
from web.routers import data as data_router
from web.routers import lab as lab_router
from web.routers import processing as processing_router
from web.routers import requests as requests_router
from web.routers import signoff as signoff_router
from web.routers import showcase as showcase_router
from web.routers import guidance as guidance_router
from web.routers import governance as governance_router
from web.routers import transfers as transfers_router
from web.routers import ai as ai_router
from web.api import api_app
from web.middleware import (
    CsrfOriginMiddleware,
    SecurityHeadersMiddleware,
    configure_logging,
)

logger = logging.getLogger("rabta")
configure_logging(LOG_LEVEL, LOG_JSON)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Fail closed before accepting traffic, then run safe startup upkeep."""

    validate_runtime_config()

    if AUTO_CREATE_SCHEMA:
        init_db()

    ready, checks = readiness_report()

    if not ready:
        raise RuntimeError(
            "Database is not release-ready; run `python -m scripts.migrate` "
            f"before startup ({checks['schema']})."
        )

    db = SessionLocal()

    try:
        expired = request_service.expire_crossmatches(
            db,
            Actor.system("startup-crossmatch-expiry"),
        )
        if expired:
            logger.info("Released %s expired crossmatch allocations", expired)
    finally:
        db.close()

    refresh_stop = asyncio.Event()
    refresh_task = None

    if INTELLIGENCE_REFRESH_ENABLED:
        from services.intelligence_refresh import worker

        refresh_task = asyncio.create_task(
            worker(
                refresh_stop,
                poll_seconds=INTELLIGENCE_REFRESH_POLL_SECONDS,
            )
        )

    try:
        yield
    finally:
        if refresh_task is not None:
            refresh_stop.set()
            await refresh_task

app = FastAPI(
    title=APP_NAME,
    description="Blood bank management and network collaboration for Pakistan.",
    version=APP_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

# Signed cookie, used only for flash messages and the language choice. Anything
# that grants access lives in the server-side session table instead, so a logout
# or an account lock takes effect immediately.
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="rh_state",
    same_site=SESSION_COOKIE_SAMESITE,
    https_only=SESSION_COOKIE_SECURE,
    max_age=security.SESSION_ABSOLUTE_HOURS * 3600,
)
app.add_middleware(CsrfOriginMiddleware)

if TRUSTED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(TRUSTED_HOSTS))

if FORCE_HTTPS:
    app.add_middleware(HTTPSRedirectMiddleware)

app.add_middleware(SecurityHeadersMiddleware)

app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).resolve().parent / "static"),
    name="static",
)

# Dedicated machine surface. Mounting a sub-application keeps the OpenAPI
# document focused on versioned JSON contracts instead of mixing in HTML forms.
app.mount("/api", api_app)

app.include_router(auth_router.router)
app.include_router(facility_router.router)
app.include_router(donors_router.router)
app.include_router(collection_router.router)
app.include_router(lab_router.router)
app.include_router(processing_router.router)
app.include_router(signoff_router.router)
app.include_router(inventory_router.router)
app.include_router(requests_router.router)
app.include_router(insights_router.router)
app.include_router(transfers_router.router)
app.include_router(emergency_router.router)
app.include_router(alerts_router.router)
app.include_router(showcase_router.router)
app.include_router(guidance_router.router)
app.include_router(data_router.router)
app.include_router(governance_router.router)
app.include_router(ai_router.router)


@app.get("/health/live", include_in_schema=False)
def liveness():
    return {
        "status": "ok",
        "service": APP_NAME,
        "version": APP_VERSION,
        "time": datetime.now(timezone.utc),
        "data_notice": DATA_NOTICE,
        "data_mode": "synthetic" if SYNTHETIC_DATA else "live",
        "scenario_date": str(DEMO_DATE) if SYNTHETIC_DATA else None,
    }


@app.get("/health/ready", include_in_schema=False)
def readiness():
    ready, checks = readiness_report()

    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "service": APP_NAME,
            "version": APP_VERSION,
            "checks": checks,
            "data_notice": DATA_NOTICE,
            "data_mode": "synthetic" if SYNTHETIC_DATA else "live",
            "scenario_date": str(DEMO_DATE) if SYNTHETIC_DATA else None,
        },
    )


@app.get("/")
def root():
    return RedirectResponse("/app/dashboard", status_code=307)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Render errors as pages, not JSON.

    A 401 sends the user to sign in with the reason stated, rather than showing a
    bare "Unauthorized" — a session that timed out mid-task should say so.
    """

    from web.templating import current_lang, flash, render
    from i18n.t import t

    lang = current_lang(request)

    # Dependencies use an HTTP exception to stop route execution when a user
    # with a temporary credential tries to enter the operational application.
    # Preserve that redirect instead of rendering the generic error page below.
    # (The normal unauthenticated redirect has its own flash message.)
    if exc.status_code in {
        status.HTTP_301_MOVED_PERMANENTLY,
        status.HTTP_302_FOUND,
        status.HTTP_303_SEE_OTHER,
        status.HTTP_307_TEMPORARY_REDIRECT,
        status.HTTP_308_PERMANENT_REDIRECT,
    } and exc.headers and exc.headers.get("Location"):
        return RedirectResponse(
            exc.headers["Location"],
            status_code=exc.status_code,
        )

    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        flash(request, t("auth.session_expired", language=lang), "warn")

        return RedirectResponse("/login", status_code=303)

    principal = None

    try:
        # Best effort: the chrome is nicer with the user's context, but an error
        # page must never fail because the lookup failed.
        from db.session import SessionLocal

        db = SessionLocal()

        try:
            principal = optional_principal(request, db)
        finally:
            db.close()
    except Exception:  # pragma: no cover
        principal = None

    if exc.status_code == status.HTTP_403_FORBIDDEN:
        template, title_key, body_key = (
            "errors/error.html",
            "errors.forbidden_title",
            "errors.forbidden_body",
        )
    elif exc.status_code == status.HTTP_404_NOT_FOUND:
        template, title_key, body_key = (
            "errors/error.html",
            "errors.not_found_title",
            "errors.not_found_body",
        )
    else:
        template, title_key, body_key = (
            "errors/error.html",
            "errors.server_title",
            "errors.server_body",
        )

    return render(
        request,
        template,
        {
            "status_code": exc.status_code,
            "error_title": t(title_key, language=lang),
            "error_body": t(body_key, language=lang),
        },
        principal=principal,
        page_title=t(title_key, language=lang),
        enabled_nav=facility_router.ENABLED_NAV,
        status_code=exc.status_code,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url.path)

    from web.templating import current_lang, render
    from i18n.t import t

    lang = current_lang(request)

    return render(
        request,
        "errors/error.html",
        {
            "status_code": 500,
            "error_title": t("errors.server_title", language=lang),
            "error_body": t("errors.server_body", language=lang),
        },
        page_title=t("errors.server_title", language=lang),
        status_code=500,
    )
