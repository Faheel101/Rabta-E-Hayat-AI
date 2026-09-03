"""Keep decision intelligence consistent with audited operational truth.

The forecast model remains a scheduled model-training concern.  This service
refreshes the *live decision snapshot* that depends on current requests and
physical unit state: demand marts, shortage risk, expiry rescue, days of cover,
facility KPIs and impact.  It never rebuilds or replaces clinical records.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from threading import Lock
from time import perf_counter
from typing import Callable

from sqlalchemy.orm import Session

from db.models import AuditLog, IntelligenceRefreshState, new_id
from db.session import SessionLocal


logger = logging.getLogger("rabta.intelligence_refresh")

STATE_ID = "decision-intelligence"
REFRESHABLE_ENTITY_TYPES = {
    "blood_request",
    "blood_unit",
    "component_production",
    "crossmatch",
    "crossmatch_batch",
    "donation",
    "emergency_incident",
    "facility",
    "import_batch",
    "lab_run",
    "transfer",
    "transfusion_record",
    "unit_issue",
}

_run_lock = Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def affects_decision_intelligence(action: str, entity_type: str) -> bool:
    """Return whether an audited mutation can change a decision snapshot."""

    if entity_type not in REFRESHABLE_ENTITY_TYPES:
        return False

    # Previewing or remapping an import does not change canonical records.
    if entity_type == "import_batch" and action != "integration.commit":
        return False

    # An onboarding draft is deliberately inactive and therefore absent from
    # every operational query. Activation is the first moment it can affect a
    # network snapshot and has its own facility audit action.
    if entity_type == "facility" and action == "network_onboarding.draft.create":
        return False

    return True


def mark_dirty_in_transaction(
    db: Session,
    *,
    action: str,
    requested_by: str,
    at: datetime | None = None,
) -> IntelligenceRefreshState:
    """Invalidate the snapshot without committing the caller's transaction."""

    stamp = at or _now()
    state = db.get(IntelligenceRefreshState, STATE_ID)

    if state is None:
        state = IntelligenceRefreshState(
            id=STATE_ID,
            status="DIRTY",
            source_version=1,
            completed_version=0,
            reason=action,
            requested_by=requested_by,
            invalidated_at=stamp,
            updated_at=stamp,
        )
        db.add(state)
        return state

    state.source_version = int(state.source_version or 0) + 1
    # A concurrent operational write must not pretend the active run stopped.
    # The finisher compares versions and returns the state to DIRTY.
    if state.status != "RUNNING":
        state.status = "DIRTY"
    state.reason = action
    state.requested_by = requested_by
    state.invalidated_at = stamp
    state.updated_at = stamp
    return state


def initialize_state(session_factory=SessionLocal) -> dict:
    """Create a durable dirty state on first deployment, preserving data."""

    db = session_factory()
    try:
        state = db.get(IntelligenceRefreshState, STATE_ID)
        if state is None:
            mark_dirty_in_transaction(
                db,
                action="STARTUP_BASELINE",
                requested_by="system:intelligence-refresh",
            )
            db.commit()
        return status_snapshot(db)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def status_snapshot(db: Session) -> dict:
    """Small presentation-safe state used by the footer and admin workspace."""

    state = db.get(IntelligenceRefreshState, STATE_ID)
    if state is None:
        return {
            "status": "UNINITIALIZED",
            "pending": True,
            "source_version": 0,
            "completed_version": 0,
            "completed_at": None,
            "invalidated_at": None,
            "duration_ms": None,
            "last_error": None,
            "reason": "STARTUP_BASELINE",
        }

    return {
        "status": state.status,
        "pending": int(state.completed_version or 0) < int(state.source_version or 0)
        or state.status in {"DIRTY", "RUNNING", "FAILED"},
        "source_version": int(state.source_version or 0),
        "completed_version": int(state.completed_version or 0),
        "completed_at": state.completed_at,
        "invalidated_at": state.invalidated_at,
        "duration_ms": state.duration_ms,
        # The UI needs an actionable failure but never a traceback or payload.
        "last_error": state.last_error,
        "reason": state.reason,
        "result": state.result_json or {},
    }


def rebuild_decision_intelligence(db: Session) -> dict:
    """Rebuild only derived decision tables, inside one database transaction."""

    from scripts.build_marts import rebuild as rebuild_marts
    from scripts.run_risk_rescue import rebuild as rebuild_risk_and_rescue
    from services.request_service import sync_clinical_demand_events

    generated_at = _now()
    clinical_result = sync_clinical_demand_events(db, now=generated_at)
    series_keys = clinical_result.pop("series_keys")
    state = db.get(IntelligenceRefreshState, STATE_ID)
    full_demand = bool(
        state is None
        or state.reason
        in {
            "STARTUP_BASELINE",
            "MANUAL_REFRESH",
            # An edit may move or remove the component/group series; a full
            # rebuild is the safe way to remove its contribution from the old
            # series as well as publish the new one.
            "BLOOD_REQUEST_UPDATED",
            "integration.commit",
        }
    )
    risk_result = rebuild_risk_and_rescue(db, generated_at=generated_at)
    mart_result = rebuild_marts(
        db,
        generated_at=generated_at,
        full_demand=full_demand,
        clinical_series_keys=series_keys,
    )
    return {
        **clinical_result,
        **risk_result,
        **mart_result,
        "generated_at": generated_at.isoformat(),
    }


def _audit_result(
    db: Session,
    *,
    action: str,
    before: dict,
    after: dict,
) -> None:
    db.add(
        AuditLog(
            id=new_id(),
            created_at=_now(),
            actor="System (intelligence-refresh) <system:intelligence-refresh>",
            action=action,
            entity_type="intelligence_refresh",
            entity_id=STATE_ID,
            before_json=before,
            after_json=after,
        )
    )


def run_pending(
    *,
    force: bool = False,
    requested_by: str = "system:intelligence-refresh",
    session_factory=SessionLocal,
    pipeline: Callable[[Session], dict] | None = None,
) -> dict:
    """Run one coalesced refresh; safe to call from worker or manual retry."""

    if not _run_lock.acquire(blocking=False):
        return {"ran": False, "status": "BUSY"}

    target_version = 0
    started = perf_counter()

    try:
        db = session_factory()
        try:
            state = db.get(IntelligenceRefreshState, STATE_ID)
            if state is None:
                state = mark_dirty_in_transaction(
                    db,
                    action="STARTUP_BASELINE",
                    requested_by=requested_by,
                )
                db.flush()

            pending = int(state.completed_version or 0) < int(state.source_version or 0)
            if not force and state.status not in {"DIRTY", "FAILED"} and not pending:
                return {"ran": False, "status": state.status}

            if force and not pending:
                state.source_version = int(state.source_version or 0) + 1
                state.invalidated_at = _now()
                state.reason = "MANUAL_REFRESH"
                state.requested_by = requested_by

            target_version = int(state.source_version or 0)
            before = {
                "status": state.status,
                "source_version": target_version,
                "completed_version": int(state.completed_version or 0),
            }
            state.status = "RUNNING"
            state.started_at = _now()
            state.failed_at = None
            state.last_error = None
            state.updated_at = state.started_at
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        pipeline_db = session_factory()
        try:
            result = (pipeline or rebuild_decision_intelligence)(pipeline_db)
            pipeline_db.commit()
        except Exception:
            pipeline_db.rollback()
            raise
        finally:
            pipeline_db.close()

        duration_ms = int((perf_counter() - started) * 1000)
        final_db = session_factory()
        try:
            state = final_db.get(IntelligenceRefreshState, STATE_ID)
            if state is None:  # pragma: no cover - destructive external drift
                raise RuntimeError("Intelligence refresh state disappeared")

            state.completed_version = max(
                int(state.completed_version or 0), target_version
            )
            state.completed_at = _now()
            state.duration_ms = duration_ms
            state.result_json = result
            state.last_error = None
            state.failed_at = None
            state.status = (
                "CLEAN"
                if int(state.source_version or 0) <= target_version
                else "DIRTY"
            )
            state.updated_at = state.completed_at
            after = {
                "status": state.status,
                "source_version": int(state.source_version or 0),
                "completed_version": int(state.completed_version or 0),
                "duration_ms": duration_ms,
                "result": result,
            }
            _audit_result(
                final_db,
                action="intelligence.refresh.completed",
                before=before,
                after=after,
            )
            final_db.commit()
            return {"ran": True, **after}
        except Exception:
            final_db.rollback()
            raise
        finally:
            final_db.close()

    except Exception as exc:
        duration_ms = int((perf_counter() - started) * 1000)
        logger.exception("Decision intelligence refresh failed")
        failure_db = session_factory()
        try:
            state = failure_db.get(IntelligenceRefreshState, STATE_ID)
            if state is not None:
                previous = {
                    "status": state.status,
                    "source_version": int(state.source_version or 0),
                    "completed_version": int(state.completed_version or 0),
                }
                state.status = "FAILED"
                state.failed_at = _now()
                state.duration_ms = duration_ms
                # Persist only the exception class. Database URLs, SQL text or
                # source payloads from the exception message belong in server
                # logs, never in an administrator-facing page.
                state.last_error = type(exc).__name__[:500]
                state.updated_at = state.failed_at
                _audit_result(
                    failure_db,
                    action="intelligence.refresh.failed",
                    before=previous,
                    after={
                        "status": "FAILED",
                        "duration_ms": duration_ms,
                        "error_type": type(exc).__name__,
                    },
                )
                failure_db.commit()
        except Exception:
            failure_db.rollback()
            logger.exception("Could not persist intelligence refresh failure")
        finally:
            failure_db.close()
        return {
            "ran": True,
            "status": "FAILED",
            "duration_ms": duration_ms,
            "error": type(exc).__name__,
        }
    finally:
        _run_lock.release()


async def worker(stop: asyncio.Event, *, poll_seconds: float = 5.0) -> None:
    """Single-process MVP worker with durable state and graceful shutdown."""

    initialize_state()
    while not stop.is_set():
        await asyncio.to_thread(run_pending)
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(1.0, poll_seconds))
        except TimeoutError:
            pass
