"""The application must stay usable while a pipeline job holds the database.

SQLite in WAL mode promises that a read never blocks behind a write. That promise
only covers readers — and this application had no read-only page, because
`optional_principal` committed a session timestamp on every authenticated GET. So
while `build_marts` or `run_forecast` held its write transaction, viewing the
dashboard stalled for the full 30-second busy timeout and then returned a 500.

An operator running a scheduled job should not take the application down.
"""

from __future__ import annotations

import sqlite3
import time

import pytest
from starlette.testclient import TestClient

from db.session import SessionLocal
from web.main import app

PASSWORD = "Rabta@2026"
OFFICER = "dr.ahmed@punjab-teaching.rabta.pk"

# Generous: the point is the difference between tens of milliseconds and the
# 30-second busy timeout, not a precise latency budget.
MAX_MS_UNDER_WRITE_LOCK = 5_000


@pytest.fixture
def client():
    with TestClient(app, follow_redirects=True) as session:
        session.post("/login", data={"email": OFFICER, "password": PASSWORD})

        # Warm the session row so the sliding window sits inside its throttle
        # interval — the steady state for a user clicking around.
        session.get("/app/dashboard")

        yield session


@pytest.fixture
def held_write_lock(scratch_path):
    """An exclusive transaction, exactly as a pipeline job holds one."""

    writer = sqlite3.connect(str(scratch_path), timeout=1)
    writer.isolation_level = None
    writer.execute("PRAGMA busy_timeout=1000")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE mart_facility_kpi SET generated_at = generated_at")

    try:
        yield writer
    finally:
        writer.execute("ROLLBACK")
        writer.close()


@pytest.mark.parametrize(
    "path", ["/app/dashboard", "/app/donors", "/app/inventory"]
)
def test_pages_still_serve_while_a_pipeline_job_holds_the_database(
    client, held_write_lock, path
):
    start = time.perf_counter()
    response = client.get(path)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert response.status_code == 200, (
        f"{path} returned {response.status_code} while a writer held the database"
    )
    assert elapsed_ms < MAX_MS_UNDER_WRITE_LOCK, (
        f"{path} took {elapsed_ms:.0f}ms under a write lock — the read path is "
        "writing again"
    )


def test_the_session_touch_is_throttled_not_removed():
    """The sliding idle window still has to slide, or a session that is in
    constant use would expire at its absolute limit and log the user out."""

    from datetime import timedelta

    from web.deps import SESSION_TOUCH_INTERVAL, _slide_idle_window

    assert SESSION_TOUCH_INTERVAL <= timedelta(minutes=5), (
        "the throttle window must stay well inside the 30-minute idle timeout"
    )

    class Row:
        last_seen_at = None
        expires_at = None

    class FakeDb:
        committed = False

        def commit(self):
            self.committed = True

        def rollback(self):
            pass

    row, db = Row(), FakeDb()
    _slide_idle_window(db, row)

    assert db.committed, "a session with no recorded last_seen must be touched"
    assert row.last_seen_at is not None
    assert row.expires_at is not None


def test_a_busy_database_does_not_log_the_user_out():
    """If the touch cannot get the lock, the session keeps its previous expiry
    rather than the request failing."""

    from sqlalchemy.exc import OperationalError

    from web.deps import _slide_idle_window

    class Row:
        last_seen_at = None
        expires_at = None

    class BusyDb:
        rolled_back = False

        def commit(self):
            raise OperationalError("UPDATE user_session", {}, Exception("locked"))

        def rollback(self):
            self.rolled_back = True

    db = BusyDb()

    _slide_idle_window(db, Row())  # must not raise

    assert db.rolled_back
