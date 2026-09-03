"""Emergency preparation stays separate from live, governed operations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import (
    Alert,
    AlertDelivery,
    AuditLog,
    BloodUnit,
    EmergencyIncident,
    Facility,
    SimulationRun,
    Transfer,
    TransferPlan,
)
from services.alert_service import (
    acknowledge_alert,
    escalate_unacknowledged,
    resolve_alert,
    upsert_alert,
)
from services.audit import Actor, ServiceError
from services.emergency_service import (
    DECLARATION_PHRASE,
    declare_incident,
    resolve_incident,
)
from services.simulation_service import compare_simulation, run_simulation


def _db(scratch_database):
    return Session(bind=scratch_database, expire_on_commit=False)


def _actor(db: Session) -> tuple[Actor, Facility]:
    facility = db.scalars(
        select(Facility).where(Facility.organization_id.is_not(None))
    ).first()
    return (
        Actor(
            user_id="emergency-coordinator",
            display_name="Dr Sara Emergency",
            role="RBC_COORDINATOR",
            facility_id=facility.id,
            organization_id=facility.organization_id,
            organization_wide=True,
        ),
        facility,
    )


def test_alert_deduplicates_cools_down_and_records_accountability(scratch_database):
    db = _db(scratch_database)
    try:
        actor, facility = _actor(db)
        moment = datetime(2026, 8, 16, 8, 0, tzinfo=timezone.utc)
        first = upsert_alert(
            db,
            actor,
            alert_type="RESERVE_BREACHED",
            severity="HIGH",
            title_en="Reserve breached",
            title_ur="محفوظ حد سے کم",
            body_en="Two units below the governed reserve.",
            body_ur="محفوظ حد سے دو یونٹس کم ہیں۔",
            facility_id=facility.id,
            now=moment,
        )
        deliveries = db.scalar(
            select(func.count()).select_from(AlertDelivery).where(
                AlertDelivery.alert_id == first.id
            )
        )
        second = upsert_alert(
            db,
            actor,
            alert_type="RESERVE_BREACHED",
            severity="HIGH",
            title_en="Reserve still breached",
            title_ur="محفوظ حد اب بھی کم",
            body_en="Fresh evidence, same operational condition.",
            body_ur="نیا ثبوت، وہی عملی حالت۔",
            facility_id=facility.id,
            now=moment + timedelta(hours=1),
        )
        assert second.id == first.id
        assert second.occurrence_count == 2
        assert (
            db.scalar(
                select(func.count()).select_from(AlertDelivery).where(
                    AlertDelivery.alert_id == first.id
                )
            )
            == deliveries
        )

        acknowledge_alert(db, actor, first.id, "Cold-room count confirmed.")
        assert first.status == "ACKNOWLEDGED"
        assert first.assigned_to == actor.display_name
        resolve_alert(db, actor, first.id, "Replacement units received and counted.")
        assert first.status == "RESOLVED"
        assert (
            db.scalar(
                select(func.count()).select_from(AuditLog).where(
                    AuditLog.entity_type == "alert",
                    AuditLog.entity_id == first.id,
                )
            )
            == 4
        )
    finally:
        db.close()


def test_critical_alert_escalates_and_tenant_boundary_is_enforced(scratch_database):
    db = _db(scratch_database)
    try:
        actor, facility = _actor(db)
        moment = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)
        alert = upsert_alert(
            db,
            actor,
            alert_type="COLD_CHAIN_BREACH",
            severity="CRITICAL",
            title_en="Cold-chain breach",
            title_ur="کولڈ چین کی خلاف ورزی",
            body_en="Shipment is quarantined pending accountable review.",
            body_ur="جواب دہ جائزے تک شپمنٹ قرنطینہ میں ہے۔",
            facility_id=facility.id,
            now=moment,
        )
        assert escalate_unacknowledged(
            db,
            Actor.system("test-alert-escalation"),
            now=moment + timedelta(minutes=61),
        ) >= 1
        assert alert.escalated_at is not None

        foreign_org = db.scalar(
            select(Facility.organization_id).where(
                Facility.organization_id.is_not(None),
                Facility.organization_id != facility.organization_id,
            )
        )
        foreign_actor = Actor(
            "foreign-alert-user",
            "Foreign Coordinator",
            "RBC_COORDINATOR",
            organization_id=foreign_org,
            organization_wide=True,
        )
        with pytest.raises(ServiceError) as refusal:
            acknowledge_alert(db, foreign_actor, alert.id, "Attempted claim")
        assert refusal.value.code == "ALERT_NOT_FOUND"
    finally:
        db.close()


def test_saved_comparison_and_live_declaration_never_move_inventory_automatically(
    scratch_database,
):
    db = _db(scratch_database)
    try:
        actor, _ = _actor(db)
        scenario = {
            "name": "Integrated emergency workflow",
            "event_type": "BUS_ACCIDENT",
            "epicenter_lat": 31.55,
            "epicenter_lon": 74.34,
            "casualties": 90,
            "severity_mix": {
                "MINOR": 0.30,
                "MODERATE": 0.30,
                "SEVERE": 0.25,
                "CRITICAL": 0.15,
            },
            "onset_profile": "RAMP_6H",
            "duration_hours": 12,
            "seed": 77,
            "iterations": 100,
            "impact_radius_km": 100,
            "facilities_degraded_pct": 20,
            "degraded_capacity_loss_pct": 35,
            "release_emergency_reserves": True,
            "emergency_reserve_release_pct": 100,
        }
        base = run_simulation(db, scenario, actor=actor)
        saved = db.get(SimulationRun, base["run_id"])
        assert saved.organization_id == actor.organization_id
        assert saved.mode == "PREPAREDNESS"
        assert base["brief_en"] and base["brief_ur"]
        assert base["infrastructure"]["degraded_facility_ids"]

        comparison = compare_simulation(
            db,
            actor,
            saved.id,
            {"roads_blocked": True, "name": "Road-disruption intervention"},
        )
        assert comparison["comparison"]["same_seed"] is True
        assert db.get(SimulationRun, comparison["run_id"]).parent_run_id == saved.id

        with pytest.raises(ServiceError) as refusal:
            declare_incident(db, actor, saved.id, "yes")
        assert refusal.value.code == "DECLARATION_ACKNOWLEDGEMENT_REQUIRED"

        statuses_before = dict(
            db.execute(
                select(BloodUnit.status, func.count()).group_by(BloodUnit.status)
            ).all()
        )
        incident = declare_incident(db, actor, saved.id, DECLARATION_PHRASE)
        plan = db.get(TransferPlan, incident.transfer_plan_id)
        transfers = list(
            db.scalars(select(Transfer).where(Transfer.plan_id == plan.id)).all()
        )
        statuses_after = dict(
            db.execute(
                select(BloodUnit.status, func.count()).group_by(BloodUnit.status)
            ).all()
        )
        assert incident.status == "ACTIVE"
        assert plan.plan_type == "EMERGENCY"
        assert statuses_after == statuses_before
        assert all(row.status == "RECOMMENDED" for row in transfers)
        assert all(row.units == len(row.unit_ids) for row in transfers)
        manifests = [unit_id for row in transfers for unit_id in row.unit_ids]
        assert len(manifests) == len(set(manifests))
        assert db.scalar(
            select(func.count()).select_from(Alert).where(
                Alert.source_entity_id == incident.id,
                Alert.alert_type == "SURGE_DETECTED",
                Alert.status == "OPEN",
            )
        ) == 1

        resolve_incident(
            db,
            actor,
            incident.id,
            "Casualty intake stabilized and routine reserves restored.",
        )
        assert db.get(EmergencyIncident, incident.id).status == "RESOLVED"
        assert db.scalar(
            select(func.count()).select_from(Alert).where(
                Alert.source_entity_id == incident.id,
                Alert.status == "RESOLVED",
            )
        ) == 1
    finally:
        db.close()
