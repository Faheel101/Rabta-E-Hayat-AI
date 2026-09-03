"""Password hashing and session lifecycle.

bcrypt is used directly rather than through passlib: passlib 1.7 reads
`bcrypt.__about__`, which bcrypt 4.1 removed, and the resulting warning on every
login is the kind of noise that hides a real error later.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import bcrypt

# Spec §13.2: session timeout 30 minutes.
SESSION_IDLE_MINUTES = 30
SESSION_ABSOLUTE_HOURS = 12

# Credential rate limiting, also §13.2.
MAX_FAILED_LOGINS = 6
LOCKOUT_MINUTES = 15

SESSION_COOKIE = "rh_session"


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False

    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # A malformed stored hash must fail closed, not raise into a 500.
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(48)


def now() -> datetime:
    return datetime.now(timezone.utc)


def session_expiry(created_at: datetime | None = None) -> datetime:
    """Idle expiry. Refreshed on each request, capped by the absolute lifetime."""

    reference = created_at or now()

    return reference + timedelta(minutes=SESSION_IDLE_MINUTES)


def absolute_deadline(created_at: datetime) -> datetime:
    return created_at + timedelta(hours=SESSION_ABSOLUTE_HOURS)


def as_aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; every comparison here is in UTC."""

    if value is None:
        return None

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_session_live(session) -> bool:
    if session is None or session.revoked_at is not None:
        return False

    current = now()
    created = as_aware(session.created_at) or current

    if current > absolute_deadline(created):
        return False

    expires = as_aware(session.expires_at)

    return expires is not None and current <= expires


def is_locked(user) -> bool:
    locked_until = as_aware(getattr(user, "locked_until", None))

    return locked_until is not None and now() < locked_until


def lockout_until() -> datetime:
    return now() + timedelta(minutes=LOCKOUT_MINUTES)
