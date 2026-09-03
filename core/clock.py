"""The single definition of "now" for this system, and UTC normalisation.

Everything is pinned to a fixed demo instant so figures cannot drift between a
rehearsal and the real run (spec §15.5, and §18's "live demo failure" risk).

`as_utc` exists because SQLite discards timezone information: a column declared
`DateTime(timezone=True)` is written as aware and read back naive, so any
comparison against an aware value raises. Normalising at the boundary is the fix;
scattering `.replace(tzinfo=...)` through templates and services is not.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

from config.settings import DEMO_DATE

DEMO_DATETIME = datetime.combine(DEMO_DATE, time(8, 0, 0), tzinfo=timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Return `value` as an aware UTC datetime, or None."""

    if value is None:
        return None

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def hours_between(start: datetime | None, end: datetime | None) -> float | None:
    left = as_utc(start)
    right = as_utc(end)

    if left is None or right is None:
        return None

    return (right - left).total_seconds() / 3600.0


def hours_until(value: datetime | None, now: datetime | None = None) -> float | None:
    """Hours from the demo instant to `value`. Negative once elapsed."""

    return hours_between(now or DEMO_DATETIME, value)


def days_until(value: datetime | None, now: datetime | None = None) -> float | None:
    hours = hours_until(value, now)

    return None if hours is None else hours / 24.0
