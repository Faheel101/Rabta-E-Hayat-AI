"""Release middleware: request identity, security headers, and CSRF boundary."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, PlainTextResponse

from config.settings import CSRF_TRUSTED_ORIGINS, IS_PRODUCTION

logger = logging.getLogger("rabta.http")

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,80}$")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _request_id(headers: Headers) -> str:
    supplied = headers.get("x-request-id", "")
    return supplied if REQUEST_ID_RE.fullmatch(supplied) else uuid.uuid4().hex


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for name in ("request_id", "method", "path", "status", "duration_ms"):
            value = getattr(record, name, None)

            if value is not None:
                payload[name] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str, json_output: bool) -> None:
    """Configure once without duplicating handlers under TestClient reloads."""

    root = logging.getLogger("rabta")
    root.setLevel(level)

    if root.handlers:
        return

    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.propagate = False


class SecurityHeadersMiddleware:
    """Attach a request id, conservative browser policy, and access log."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = _request_id(headers)
        started = time.perf_counter()
        status_code = 500

        async def send_with_headers(message):
            nonlocal status_code

            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_headers = MutableHeaders(scope=message)
                response_headers["x-request-id"] = request_id
                response_headers["x-content-type-options"] = "nosniff"
                response_headers["x-frame-options"] = "DENY"
                response_headers["referrer-policy"] = "strict-origin-when-cross-origin"
                response_headers["permissions-policy"] = (
                    "camera=(), microphone=(), geolocation=(), payment=()"
                )
                response_headers["content-security-policy"] = (
                    "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
                    "form-action 'self'; img-src 'self' data:; font-src 'self'; "
                    "connect-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "script-src 'self' 'unsafe-inline' 'unsafe-eval'"
                )

                if IS_PRODUCTION:
                    response_headers["strict-transport-security"] = (
                        "max-age=31536000; includeSubDomains"
                    )

            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            logger.info(
                "%s %s %s %.1fms",
                scope.get("method"),
                scope.get("path"),
                status_code,
                duration_ms,
                extra={
                    "request_id": request_id,
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": status_code,
                    "duration_ms": duration_ms,
                },
            )


def _normalise_origin(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


class CsrfOriginMiddleware:
    """Reject cross-site browser writes while preserving server API clients.

    Modern browsers send ``Origin`` and ``Sec-Fetch-Site`` on unsafe form
    requests.  Combined with strict SameSite session cookies, validating both is
    a strong CSRF boundary that also covers every existing and future form
    without asking individual route authors to remember a token field.  Clients
    such as an HL7 bridge may omit browser headers and continue to authenticate
    with their scoped API key.
    """

    def __init__(self, app):
        self.app = app
        self.trusted = {
            origin for value in CSRF_TRUSTED_ORIGINS if (origin := _normalise_origin(value))
        }

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        fetch_site = (headers.get("sec-fetch-site") or "").lower()
        origin_value = headers.get("origin")
        referer_value = headers.get("referer")
        host = (headers.get("host") or "").lower()
        same_origin = f"{scope.get('scheme', 'http')}://{host}"
        allowed = {same_origin, *self.trusted}

        rejected = fetch_site == "cross-site"

        for value in (origin_value, referer_value):
            if value and _normalise_origin(value) not in allowed:
                rejected = True

        if rejected:
            response = (
                JSONResponse(
                    {"error": {"code": "CSRF_ORIGIN", "message": "Cross-site write refused."}},
                    status_code=403,
                )
                if scope.get("path", "").startswith("/api/")
                else PlainTextResponse("Cross-site write refused.", status_code=403)
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
