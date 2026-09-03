"""Column types that make timezone handling impossible to get wrong.

SQLite has no native timestamp type. A column declared `DateTime(timezone=True)`
is written as an aware value and read back *naive*, so any comparison against a
computed aware value raises `can't subtract offset-naive and offset-aware
datetimes`. That bug landed three times in this codebase — a 500 on the
inventory page, a crash in the donor backfill, and once in the engines — each
time patched at the call site, which is not a fix.

`UtcDateTime` fixes the class rather than the instance: everything read from the
database is aware and in UTC, so no caller ever has to remember. PostgreSQL does
this natively; this makes SQLite behave the same way, which also means the
migration will not change behaviour.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator):
    """A timezone-aware UTC datetime, guaranteed on the way out."""

    impl = DateTime
    cache_ok = True

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("timezone", True)
        super().__init__(*args, **kwargs)

    def process_bind_param(self, value, dialect):
        """On write: normalise to UTC.

        A naive value is treated as UTC rather than rejected. Every writer in
        this system already works in UTC, and pandas hands back naive timestamps
        from several code paths; raising here would convert a harmless
        inconsistency into a failed pipeline run.
        """

        if value is None:
            return None

        if not isinstance(value, datetime):
            return value

        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)

        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        """On read: always aware, always UTC. This is the whole point."""

        if value is None:
            return None

        if not isinstance(value, datetime):
            return value

        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)

        return value.astimezone(timezone.utc)
