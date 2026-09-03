"""Every datetime read from the database is aware and in UTC.

Regression guard for a bug class that landed three times: SQLite discards
timezone information, so a column declared `DateTime(timezone=True)` came back
naive and any comparison against a computed aware value raised. Patching call
sites did not stop it recurring; `db.types.UtcDateTime` does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from core.clock import DEMO_DATETIME
from db.base import Base
from db.models import BloodUnit, Donor, UserAccount
from db.session import SessionLocal
from db.types import UtcDateTime


def test_no_model_declares_a_raw_timezone_datetime():
    """A new column added as DateTime(timezone=True) would reintroduce the bug."""

    from sqlalchemy import DateTime

    offenders = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, DateTime)
        and not isinstance(column.type, UtcDateTime)
    ]

    assert not offenders, (
        f"these columns bypass UtcDateTime and will read back naive: {offenders}"
    )


@pytest.fixture(scope="module")
def db():
    session = SessionLocal()

    try:
        yield session
    finally:
        session.close()


def test_blood_unit_timestamps_come_back_aware(db):
    unit = db.scalars(select(BloodUnit).limit(1)).first()

    assert unit is not None, "no units to check"

    for field in ("collected_at", "expires_at"):
        value = getattr(unit, field)

        assert value is not None
        assert value.tzinfo is not None, f"{field} came back naive"
        assert value.utcoffset() == timedelta(0), f"{field} is not UTC"


def test_arithmetic_against_the_demo_instant_does_not_raise(db):
    """The exact operation that produced the 500 on the inventory page."""

    unit = db.scalars(select(BloodUnit).limit(1)).first()

    delta = unit.expires_at - DEMO_DATETIME

    assert isinstance(delta, timedelta)


def test_donor_last_donation_is_aware(db):
    donor = db.scalars(
        select(Donor).where(Donor.last_donation_at.is_not(None)).limit(1)
    ).first()

    if donor is None:
        pytest.skip("no donor with a recorded donation")

    assert donor.last_donation_at.tzinfo is not None
    assert (DEMO_DATETIME - donor.last_donation_at).days >= 0


def test_user_created_at_is_aware(db):
    user = db.scalars(select(UserAccount).limit(1)).first()

    assert user is not None
    assert user.created_at.tzinfo is not None


def test_naive_value_written_is_read_back_as_utc(db):
    """Writers that hand over a naive value get UTC semantics, not a crash."""

    decorator = UtcDateTime()

    naive = datetime(2026, 8, 6, 8, 0, 0)
    bound = decorator.process_bind_param(naive, None)

    assert bound.tzinfo is not None
    assert bound.utcoffset() == timedelta(0)

    result = decorator.process_result_value(naive, None)

    assert result.tzinfo is not None
    assert result.utcoffset() == timedelta(0)


def test_non_utc_value_is_converted_not_relabelled():
    """A +05:00 Pakistan time must become the same instant in UTC, not the same
    clock face with a different label."""

    decorator = UtcDateTime()
    pkt = timezone(timedelta(hours=5))

    value = datetime(2026, 8, 6, 13, 0, 0, tzinfo=pkt)
    bound = decorator.process_bind_param(value, None)

    assert bound.hour == 8
    assert bound.utcoffset() == timedelta(0)
