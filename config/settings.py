"""Runtime configuration with fail-closed production validation.

Development and the synthetic Docker demonstration remain easy to start, but a
deployment that calls itself production must provide its own cryptographic
secret, trusted hosts, and secure cookies.  Keeping those checks here makes the
same contract apply to Uvicorn, release checks, tests, and future workers.
"""

import os
from datetime import date
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")

DEFAULT_DB_URL = f"sqlite:///{(BASE_DIR / 'rabta.db').as_posix()}"

DATABASE_URL = os.getenv("DATABASE_URL") or DEFAULT_DB_URL
DEMO_DATE = date.fromisoformat(os.getenv("DEMO_DATE") or "2026-08-06")
APP_NAME = os.getenv("APP_NAME") or "Rabta-e-Hayat"
APP_VERSION = os.getenv("APP_VERSION") or "0.15.0"
APP_ENV = (os.getenv("APP_ENV") or "development").strip().lower()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)

    if raw is None:
        return default

    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(
        item.strip() for item in (os.getenv(name) or default).split(",") if item.strip()
    )


IS_PRODUCTION = APP_ENV == "production"
SECRET_KEY = os.getenv("SECRET_KEY") or (
    "dev-only-change-me" if not IS_PRODUCTION else ""
)
SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", IS_PRODUCTION)
SESSION_COOKIE_SAMESITE = (os.getenv("SESSION_COOKIE_SAMESITE") or "strict").lower()
FORCE_HTTPS = _bool("FORCE_HTTPS", False)
AUTO_CREATE_SCHEMA = _bool("AUTO_CREATE_SCHEMA", not IS_PRODUCTION)
TRUSTED_HOSTS = _csv(
    "TRUSTED_HOSTS",
    "127.0.0.1,localhost,testserver" if not IS_PRODUCTION else "",
)
CSRF_TRUSTED_ORIGINS = _csv("CSRF_TRUSTED_ORIGINS")
LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
LOG_JSON = _bool("LOG_JSON", IS_PRODUCTION)
DATA_NOTICE = os.getenv("DATA_NOTICE") or "Synthetic demonstration data only"
SYNTHETIC_DATA = _bool("SYNTHETIC_DATA", APP_ENV != "production")
INTELLIGENCE_REFRESH_ENABLED = _bool(
    "INTELLIGENCE_REFRESH_ENABLED", APP_ENV != "test"
)
INTELLIGENCE_REFRESH_POLL_SECONDS = max(
    1.0, float(os.getenv("INTELLIGENCE_REFRESH_POLL_SECONDS") or "5")
)

# Governed Qwen assistance. The operational platform remains fully functional
# when this is disabled, unconfigured, rate-limited, or unreachable.
AI_ENABLED = _bool("AI_ENABLED", True)
AI_PROVIDER = "qwen"
QWEN_API_KEY = os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or ""
QWEN_MODEL = os.getenv("QWEN_MODEL") or "qwen3.7-plus"
QWEN_BASE_URL = (
    os.getenv("QWEN_BASE_URL")
    or "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
).rstrip("/")
AI_TIMEOUT_SECONDS = min(
    30.0, max(1.0, float(os.getenv("AI_TIMEOUT_SECONDS") or "8"))
)
AI_MAX_RETRIES = min(1, max(0, int(os.getenv("AI_MAX_RETRIES") or "1")))
AI_MAX_INPUT_CHARS = min(
    50_000, max(2_000, int(os.getenv("AI_MAX_INPUT_CHARS") or "20000"))
)
AI_MAX_QUESTION_CHARS = min(
    1_000, max(120, int(os.getenv("AI_MAX_QUESTION_CHARS") or "500"))
)
AI_DAILY_TOKEN_BUDGET = max(
    1_000, int(os.getenv("AI_DAILY_TOKEN_BUDGET") or "250000")
)
AI_DAILY_BUDGET_USD = max(
    0.0, float(os.getenv("AI_DAILY_BUDGET_USD") or "20")
)
# Configurable estimate used only for the budget/usage display. Provider
# billing remains authoritative and can vary by region, model and tier.
AI_INPUT_USD_PER_MILLION = max(
    0.0, float(os.getenv("AI_INPUT_USD_PER_MILLION") or "0.40")
)
AI_OUTPUT_USD_PER_MILLION = max(
    0.0, float(os.getenv("AI_OUTPUT_USD_PER_MILLION") or "1.20")
)
AI_CACHE_TTL_SECONDS = min(
    86_400, max(60, int(os.getenv("AI_CACHE_TTL_SECONDS") or "3600"))
)
AI_CIRCUIT_FAILURE_THRESHOLD = min(
    10, max(1, int(os.getenv("AI_CIRCUIT_FAILURE_THRESHOLD") or "3"))
)
AI_CIRCUIT_RESET_SECONDS = min(
    3_600, max(30, int(os.getenv("AI_CIRCUIT_RESET_SECONDS") or "120"))
)
AI_FEATURES = frozenset(
    _csv(
        "AI_FEATURES",
        "command_brief,transfer_rationale,emergency_brief,ask_rabta,forecast_guardian,optimizer_advisor",
    )
)


def runtime_config_issues(environ: Mapping[str, str] | None = None) -> list[str]:
    """Return unsafe production settings without exposing their values.

    ``environ`` keeps the validation independently testable.  Runtime startup
    passes the real environment; release tests pass small explicit mappings.
    """

    env = dict(os.environ if environ is None else environ)
    app_env = (env.get("APP_ENV") or "development").strip().lower()

    if app_env not in {"development", "test", "demo", "production"}:
        return ["APP_ENV must be development, test, demo, or production"]

    if app_env != "production":
        return []

    issues: list[str] = []
    secret = env.get("SECRET_KEY") or ""
    hosts = [item.strip() for item in (env.get("TRUSTED_HOSTS") or "").split(",") if item.strip()]
    secure_cookie = (env.get("SESSION_COOKIE_SECURE") or "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    demo_logins = (env.get("RABTA_SHOW_DEMO_LOGINS") or "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    if len(secret) < 32 or secret == "dev-only-change-me":
        issues.append("SECRET_KEY must be an unpredictable value of at least 32 characters")

    if not secure_cookie:
        issues.append("SESSION_COOKIE_SECURE must be enabled in production")

    if not hosts or "*" in hosts:
        issues.append("TRUSTED_HOSTS must explicitly list production hostnames")

    if demo_logins:
        issues.append("RABTA_SHOW_DEMO_LOGINS must be disabled in production")

    ai_enabled = (env.get("AI_ENABLED") or "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    qwen_key = env.get("QWEN_API_KEY") or env.get("DASHSCOPE_API_KEY") or ""
    qwen_base = env.get("QWEN_BASE_URL") or QWEN_BASE_URL
    if ai_enabled and qwen_key and not qwen_base.startswith("https://"):
        issues.append("QWEN_BASE_URL must use HTTPS when Qwen is configured")

    same_site = (env.get("SESSION_COOKIE_SAMESITE") or "strict").lower()

    if same_site not in {"lax", "strict"}:
        issues.append("SESSION_COOKIE_SAMESITE must be lax or strict")

    return issues


def validate_runtime_config(environ: Mapping[str, str] | None = None) -> None:
    issues = runtime_config_issues(environ)

    if issues:
        raise RuntimeError("Unsafe runtime configuration: " + "; ".join(issues))
