"""Version and recovery invariants for operational decision refreshes."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.base import Base
from config.settings import DEMO_DATE
from db.models import (
    AuditLog,
    BloodGroup,
    BloodUnit,
    Component,
    Donation,
    Facility,
    IntelligenceRefreshState,
    InventorySnapshot,
    MartDailyDemand,
    MartImpact,
    Organization,
    PlatformSetting,
)
from services.audit import Actor, audited
from services.intelligence_refresh import (
    STATE_ID,
    mark_dirty_in_transaction,
    run_pending,
    status_snapshot,
)


def _maker():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def test_dirty_snapshot_refreshes_exact_source_version_and_is_audited():
    engine, maker = _maker()
    db = maker()
    try:
        mark_dirty_in_transaction(
            db,
            action="BLOOD_REQUEST_CREATED",
            requested_by="Test Officer <officer-one>",
        )
        db.commit()
        assert status_snapshot(db)["pending"] is True

        result = run_pending(
            session_factory=maker,
            pipeline=lambda pipeline_db: {"derived_rows": 17},
        )
        db.expire_all()
        state = db.get(IntelligenceRefreshState, STATE_ID)
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "intelligence.refresh.completed"
            )
        )

        assert result["status"] == "CLEAN"
        assert state.source_version == 1
        assert state.completed_version == 1
        assert state.result_json == {"derived_rows": 17}
        assert audit is not None
    finally:
        db.close()
        engine.dispose()


def test_write_during_refresh_is_not_lost():
    engine, maker = _maker()
    seed = maker()
    try:
        mark_dirty_in_transaction(
            seed,
            action="UNIT_ISSUED",
            requested_by="Test Officer <officer-one>",
        )
        seed.commit()
    finally:
        seed.close()

    def invalidate_while_running(pipeline_db):
        mark_dirty_in_transaction(
            pipeline_db,
            action="UNIT_RETURN_ACCEPTED",
            requested_by="Test Officer <officer-one>",
        )
        return {"derived_rows": 5}

    try:
        first = run_pending(session_factory=maker, pipeline=invalidate_while_running)
        check = maker()
        try:
            state = check.get(IntelligenceRefreshState, STATE_ID)
            assert first["status"] == "DIRTY"
            assert state.source_version == 2
            assert state.completed_version == 1
        finally:
            check.close()

        second = run_pending(
            session_factory=maker,
            pipeline=lambda pipeline_db: {"derived_rows": 6},
        )
        assert second["status"] == "CLEAN"
        assert second["source_version"] == second["completed_version"] == 2
    finally:
        engine.dispose()


def test_failed_refresh_is_visible_and_recovers_on_retry():
    engine, maker = _maker()
    seed = maker()
    try:
        mark_dirty_in_transaction(
            seed,
            action="DONATION_COLLECTED",
            requested_by="Test Phlebotomist <phleb-one>",
        )
        seed.commit()
    finally:
        seed.close()

    def broken_pipeline(pipeline_db):
        raise RuntimeError("synthetic refresh failure")

    try:
        failed = run_pending(session_factory=maker, pipeline=broken_pipeline)
        check = maker()
        try:
            state = check.get(IntelligenceRefreshState, STATE_ID)
            assert failed["status"] == "FAILED"
            assert state.status == "FAILED"
            assert state.completed_version == 0
            assert state.last_error == "RuntimeError"
        finally:
            check.close()

        recovered = run_pending(
            session_factory=maker,
            pipeline=lambda pipeline_db: {"derived_rows": 9},
        )
        assert recovered["status"] == "CLEAN"
        assert recovered["source_version"] == recovered["completed_version"] == 1
    finally:
        engine.dispose()


def test_unrelated_audited_setting_does_not_trigger_refresh_loop():
    engine, maker = _maker()
    db = maker()
    try:
        actor = Actor.system("test-governance")
        with audited(
            db,
            actor,
            "optimizer.weights.update",
            "platform_setting",
            "optimizer.weights",
        ) as entry:
            setting = PlatformSetting(
                key="optimizer.weights",
                value_json={"shortage": 1000},
                updated_by=actor.user_id,
            )
            db.add(setting)
            entry.on(setting, after={"shortage": 1000})

        assert db.get(IntelligenceRefreshState, STATE_ID) is None
    finally:
        db.close()
        engine.dispose()


def test_impact_uses_canonical_demand_and_live_operational_day():
    engine, maker = _maker()
    db = maker()
    day_start = datetime.combine(DEMO_DATE, time.min, tzinfo=timezone.utc)
    try:
        db.add_all(
            [
                Organization(id="org-one", code="ORG1", name_en="Test"),
                Facility(
                    id="facility-one",
                    code="FAC1",
                    organization_id="org-one",
                    name_en="Test Blood Bank",
                    district="Lahore",
                ),
                BloodGroup(id=1, code="A+", abo="A", rh="+"),
                Component(
                    id=1,
                    code="PRBC",
                    name_en="Packed red blood cells",
                    shelf_life_days=42,
                    storage_temp_min_c=2,
                    storage_temp_max_c=6,
                ),
            ]
        )
        db.flush()
        db.add(
            InventorySnapshot(
                snapshot_date=DEMO_DATE - timedelta(days=1),
                facility_id="facility-one",
                component_id=1,
                blood_group_id=1,
                units_collected=10,
                # Deliberately inconsistent: canonical demand must win.
                units_issued=99,
                units_expired=1,
                units_discarded=1,
            )
        )
        db.add_all(
            [
                MartDailyDemand(
                    facility_id="facility-one",
                    component_id=1,
                    blood_group_id=1,
                    demand_date=DEMO_DATE - timedelta(days=1),
                    units_requested=10,
                    units_issued=8,
                    units_unmet=2,
                ),
                MartDailyDemand(
                    facility_id="facility-one",
                    component_id=1,
                    blood_group_id=1,
                    demand_date=DEMO_DATE,
                    units_requested=2,
                    units_issued=1,
                    units_unmet=1,
                ),
                Donation(
                    id="donation-current",
                    din="ZAA26000000000001",
                    donor_id="donor-one",
                    facility_id="facility-one",
                    collected_at=day_start + timedelta(hours=8),
                ),
                BloodUnit(
                    id="unit-expired-current",
                    din="ZAA26000000000002",
                    facility_id="facility-one",
                    component_id=1,
                    blood_group_id=1,
                    collected_at=day_start - timedelta(days=30),
                    expires_at=day_start,
                    status="DISCARDED",
                    discarded_at=day_start + timedelta(hours=8),
                    discard_reason="EXPIRY",
                ),
                BloodUnit(
                    id="unit-discarded-current",
                    din="ZAA26000000000003",
                    facility_id="facility-one",
                    component_id=1,
                    blood_group_id=1,
                    collected_at=day_start - timedelta(days=2),
                    expires_at=day_start + timedelta(days=30),
                    status="DISCARDED",
                    discarded_at=day_start + timedelta(hours=9),
                    discard_reason="BROKEN_COLD_CHAIN",
                ),
            ]
        )
        db.flush()

        from scripts.build_marts import build_impact

        assert build_impact(db) == 2
        previous = db.scalar(
            select(MartImpact).where(
                MartImpact.impact_date == DEMO_DATE - timedelta(days=1)
            )
        )
        current = db.scalar(
            select(MartImpact).where(MartImpact.impact_date == DEMO_DATE)
        )

        assert previous.units_issued == 8
        assert current.units_collected == 1
        assert current.units_issued == 1
        assert current.units_expired == 1
        assert current.units_discarded == 1
        assert current.fill_rate == 0.5
    finally:
        db.close()
        engine.dispose()
