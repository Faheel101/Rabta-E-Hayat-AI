"""Shared test fixtures.

The important one here is `scratch_database`. Several test modules exercise the
service layer, which commits deliberately — an audit entry and the change it
records share a transaction — so a session-level rollback discards nothing and a
savepoint-backed transaction does not help either, because pysqlite defers its
BEGIN until the first statement. Those modules therefore run against a copy.

Making that copy correctly matters more than it looks. `rabta.db` runs in WAL
mode, so recent commits live in `rabta.db-wal` until a checkpoint folds them
back. Copying the `.db` file on its own captures a database as of the last
checkpoint and silently loses everything since — which showed up as tests that
passed alone and failed in a suite, on data that was simply not there.

`sqlite3.Connection.backup()` is the supported way: it takes a consistent
snapshot including the WAL, without needing to know where the sidecar files are.
"""

from __future__ import annotations

import sqlite3
import pathlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# CI/release validation may point at a pre-migrated disposable source. The
# default remains the repository demo database for ordinary local runs.
SOURCE = Path(os.getenv("RABTA_TEST_SOURCE_DB", "rabta.db"))
_TEST_DIRECTORY: Path | None = None
_TEST_DATABASE: Path | None = None


def copy_database(target: Path) -> Path:
    """A consistent snapshot of the demo database at `target`.

    Uses the backup API rather than a file copy so WAL contents come with it.
    """

    source = sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True)
    destination = sqlite3.connect(str(target))

    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    return target


def pytest_configure(config):
    """Point every application import at one disposable database snapshot.

    This hook runs before test modules are collected, which is the only reliable
    moment to set DATABASE_URL: many web tests import the global application and
    SessionLocal at module scope. Dependency overrides applied later cannot
    protect login, session and audit writes performed through those imports.
    """

    global _TEST_DIRECTORY, _TEST_DATABASE
    _TEST_DIRECTORY = Path(tempfile.mkdtemp(prefix="rabta-test-suite-"))
    _TEST_DATABASE = copy_database(_TEST_DIRECTORY / "scratch.db")
    os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DATABASE}"
    os.environ.setdefault("APP_ENV", "test")


def pytest_unconfigure(config):
    global _TEST_DIRECTORY
    if _TEST_DIRECTORY is not None:
        shutil.rmtree(_TEST_DIRECTORY, ignore_errors=True)
        _TEST_DIRECTORY = None


@pytest.fixture(scope="module")
def session():
    """A read-only session against the demo database.

    Used by the generator, simulator and transfer-plan tests, which assert on the
    data the pipeline produced rather than writing anything. Nothing here
    commits, so no isolation is needed — unlike the service tests, which do.
    """

    from db.session import SessionLocal

    db = SessionLocal()

    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture(scope="module")
def facilities(session):
    """Every active facility, for the reserve-policy invariants."""

    from sqlalchemy import select

    from db.models import Facility

    return list(
        session.scalars(
            select(Facility).where(Facility.is_active.is_(True)).order_by(Facility.code)
        ).all()
    )


@pytest.fixture(scope="module")
def compatibility(session):
    """The seeded matrix: (component, recipient, donor) -> (rank, needs_override).

    Keyed rather than listed, so a test can assert a pairing is ABSENT as
    directly as it asserts one is present. An incompatible pairing that is merely
    missing from a list reads the same as one nobody thought about, and the
    difference matters — the platelet defect this suite exists to catch was
    exactly a set of pairings marked compatible that should not have been.
    """

    from sqlalchemy import select

    from db.models import BloodGroup, Compatibility, Component

    groups = {row.id: row.code for row in session.scalars(select(BloodGroup)).all()}

    rows = session.execute(
        select(
            Component.code,
            Compatibility.recipient_group_id,
            Compatibility.donor_group_id,
            Compatibility.preference_rank,
            Compatibility.requires_override,
        )
        .select_from(Compatibility)
        .join(Component, Component.id == Compatibility.component_id)
        .where(Compatibility.is_compatible.is_(True))
    ).all()

    return {
        (component, groups[recipient_id], groups[donor_id]): (
            rank,
            bool(needs_override),
        )
        for component, recipient_id, donor_id, rank, needs_override in rows
    }


@pytest.fixture(scope="session")
def scratch_path(tmp_path_factory):
    """One copy of the demo database per test run, deleted when it ends.

    Session-scoped and shared deliberately. rabta.db is 966 MB, so a copy per
    module across three retained pytest runs filled the disk — 100% full, and
    pytest died writing its own cache. One copy, removed at the end, keeps that
    from happening again.

    The tradeoff is that modules sharing this file see each other's writes. That
    is acceptable because each test creates the records it asserts on; a test
    that counted rows globally would not be safe here, and should not be
    written that way regardless.
    """

    directory = Path(tempfile.mkdtemp(prefix="rabta-flow-tests-"))
    target = copy_database(directory / "scratch.db")
    try:
        yield target
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(scope="session")
def scratch_database(scratch_path):
    """A throwaway SQLAlchemy engine over the shared copy."""

    from sqlalchemy import create_engine

    engine = create_engine(
        f"sqlite:///{scratch_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    try:
        yield engine
    finally:
        engine.dispose()
