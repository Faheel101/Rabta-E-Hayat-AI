"""Declaration boundary between a preparedness simulation and live operations.

Running a model changes nothing. Declaring its result creates an accountable
incident and a physical-unit FEFO transfer plan, but still leaves every movement
as a recommendation requiring source approval. This preserves the platform's
central safety rule: an algorithm never moves blood by itself.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import Permission
from core import config, geo, policy
from core.clock import as_utc
from db.models import (
    Alert,
    BloodGroup,
    BloodUnit,
    Component,
    EmergencyIncident,
    Facility,
    Organization,
    SimulationRun,
    Transfer,
    TransferPlan,
    new_id,
)
from services.alert_service import OPEN_STATUSES, resolve_alert, upsert_alert
from services.audit import Actor, ServiceError, audited, require, snapshot
from services.common import DEMO_DATETIME, clear_caches
from services.simulation_service import get_simulation_run
from services.transfer_service import ACTIVE_TRANSFER_STATUSES

DECLARATION_PHRASE = "DECLARE LIVE RESPONSE"
INCIDENT_FIELDS = (
    "simulation_run_id",
    "organization_id",
    "transfer_plan_id",
    "title",
    "event_type",
    "status",
    "declared_by",
    "declared_at",
    "resolved_by",
    "resolved_at",
    "resolution_note",
)


def _sharing_allowed(
    db: Session,
    source: Facility,
    destination: Facility,
) -> bool:
    if source.organization_id == destination.organization_id:
        return True
    source_org = db.get(Organization, source.organization_id)
    destination_org = db.get(Organization, destination.organization_id)
    return bool(
        source_org
        and destination_org
        and source_org.network_opt_in
        and destination_org.network_opt_in
        and source.shares_inventory
        and destination.shares_inventory
    )


def _claimed_unit_ids(db: Session) -> set[str]:
    claimed: set[str] = set()
    rows = db.scalars(
        select(Transfer.unit_ids).where(
            Transfer.status.in_(list(ACTIVE_TRANSFER_STATUSES))
        )
    ).all()
    for unit_ids in rows:
        claimed.update(unit_ids or [])
    return claimed


def _physical_recommendations(
    db: Session,
    run: SimulationRun,
    plan: TransferPlan,
    *,
    now: datetime,
    reserve_hold_factor: float,
) -> list[Transfer]:
    results = dict(run.results_json or {})
    claimed = _claimed_unit_ids(db)
    assigned: set[str] = set()
    facilities = {
        item.id: item for item in db.scalars(select(Facility)).all()
    }
    components = {
        item.id: item for item in db.scalars(select(Component)).all()
    }
    groups = {
        item.id: item for item in db.scalars(select(BloodGroup)).all()
    }
    handling_buffer = float(config.get("expiry.handling_buffer_hours", 12))
    recommendations: list[Transfer] = []

    for suggestion in results.get("emergency_transfers") or []:
        source = facilities.get(suggestion.get("from_facility_id"))
        destination = facilities.get(suggestion.get("to_facility_id"))
        component = components.get(int(suggestion.get("component_id") or 0))
        donor_group = groups.get(int(suggestion.get("blood_group_id") or 0))
        recipient_group_id = int(
            suggestion.get("recipient_group_id")
            or suggestion.get("blood_group_id")
            or 0
        )
        if not source or not destination or not component or not donor_group:
            continue
        if not _sharing_allowed(db, source, destination):
            continue

        travel_minutes = int(suggestion.get("travel_minutes") or 0)
        if travel_minutes <= 0:
            distance = geo.haversine_km(
                float(source.latitude),
                float(source.longitude),
                float(destination.latitude),
                float(destination.longitude),
            )
            travel_minutes = geo.travel_minutes_from_distance(distance)
        else:
            distance = geo.haversine_km(
                float(source.latitude),
                float(source.longitude),
                float(destination.latitude),
                float(destination.longitude),
            )
        if travel_minutes > float(component.max_transport_hours or 24) * 60:
            continue

        expiry_cutoff = now + timedelta(
            minutes=travel_minutes,
            hours=handling_buffer,
        )
        available_count = int(
            db.scalar(
                select(func.count()).select_from(BloodUnit).where(
                    BloodUnit.facility_id == source.id,
                    BloodUnit.component_id == component.id,
                    BloodUnit.blood_group_id == donor_group.id,
                    BloodUnit.status == "AVAILABLE",
                    BloodUnit.screening_status == "PASSED",
                    BloodUnit.cold_chain_breach_count == 0,
                    BloodUnit.expires_at > now,
                )
            )
            or 0
        )
        floor = math.ceil(
            policy.reserve_floor(
                source,
                component.code,
                donor_group.code,
            )
            * reserve_hold_factor
        )
        movable = max(0, available_count - floor)
        requested = min(int(suggestion.get("units") or 0), movable)
        if requested <= 0:
            continue

        candidates = list(
            db.scalars(
                select(BloodUnit)
                .where(
                    BloodUnit.facility_id == source.id,
                    BloodUnit.component_id == component.id,
                    BloodUnit.blood_group_id == donor_group.id,
                    BloodUnit.status == "AVAILABLE",
                    BloodUnit.screening_status == "PASSED",
                    BloodUnit.cold_chain_breach_count == 0,
                    BloodUnit.expires_at > expiry_cutoff,
                )
                .order_by(BloodUnit.expires_at, BloodUnit.din)
            ).all()
        )
        selected = [
            unit
            for unit in candidates
            if unit.id not in claimed and unit.id not in assigned
        ][:requested]
        if not selected:
            continue
        assigned.update(unit.id for unit in selected)

        recommendations.append(
            Transfer(
                id=new_id(),
                plan_id=plan.id,
                from_facility_id=source.id,
                to_facility_id=destination.id,
                component_id=component.id,
                blood_group_id=donor_group.id,
                recipient_group_id=recipient_group_id,
                units=len(selected),
                status="RECOMMENDED",
                unit_ids=[unit.id for unit in selected],
                est_travel_minutes=travel_minutes,
                distance_km=round(distance, 1),
                transport_mode="ROAD",
                rationale_en=(
                    f"Live incident {run.name}: move {len(selected)} FEFO "
                    f"{donor_group.code} {component.code} units to "
                    f"{destination.name_en}; source approval is still required."
                ),
                rationale_ur=(
                    f"براہِ راست ہنگامی واقعہ {run.name}: {donor_group.code} "
                    f"{component.code} کے {len(selected)} پہلے ختم ہونے والے یونٹس "
                    f"{destination.name_en} منتقل کرنے کی سفارش؛ ماخذ کی منظوری لازم ہے۔"
                ),
                projected_shortage_averted=float(len(selected)),
                created_at=now,
                recommended_at=now,
            )
        )

    return recommendations


def declare_incident(
    db: Session,
    actor: Actor,
    run_id: str,
    acknowledgement: str,
    *,
    now: datetime = DEMO_DATETIME,
) -> EmergencyIncident:
    require(actor, Permission.DECLARE_EMERGENCY, "declare a live emergency")
    if (acknowledgement or "").strip().upper() != DECLARATION_PHRASE:
        raise ServiceError(
            "DECLARATION_ACKNOWLEDGEMENT_REQUIRED",
            f"Type {DECLARATION_PHRASE} to confirm the live response.",
            field="acknowledgement",
        )
    run = get_simulation_run(db, actor, run_id)
    existing = db.scalar(
        select(EmergencyIncident).where(
            EmergencyIncident.simulation_run_id == run.id
        )
    )
    if existing is not None:
        raise ServiceError(
            "INCIDENT_ALREADY_DECLARED",
            "This preparedness run has already been declared.",
        )
    organization_id = run.organization_id or actor.organization_id
    if not organization_id:
        raise ServiceError(
            "INCIDENT_ORGANIZATION_REQUIRED",
            "A live incident must belong to an accountable organization.",
        )
    now = as_utc(now) or DEMO_DATETIME
    scenario = dict(run.scenario_json or {})
    release_pct = (
        float(scenario.get("emergency_reserve_release_pct", 0))
        if scenario.get("release_emergency_reserves")
        else 0.0
    )
    reserve_hold_factor = max(0.0, 1.0 - release_pct / 100.0)
    plan = TransferPlan(
        id=new_id(),
        created_at=now,
        plan_type="EMERGENCY",
        status="GENERATED",
        scope="PROVINCE",
        parameters_json={
            "simulation_run_id": run.id,
            "live_incident": True,
            "reserve_hold_factor": reserve_hold_factor,
            "reserve_release_pct": release_pct,
            "human_approval_required": True,
        },
        created_by=actor.display_name,
    )
    incident = EmergencyIncident(
        id=new_id(),
        simulation_run_id=run.id,
        organization_id=organization_id,
        transfer_plan_id=plan.id,
        title=run.name,
        event_type=run.event_type,
        status="ACTIVE",
        declared_by=actor.display_name,
        declared_at=now,
        created_at=now,
    )
    recommendations = _physical_recommendations(
        db,
        run,
        plan,
        now=now,
        reserve_hold_factor=reserve_hold_factor,
    )
    with audited(
        db,
        actor,
        "emergency.declare",
        "emergency_incident",
        incident.id,
    ) as entry:
        db.add(plan)
        db.add(incident)
        db.add_all(recommendations)
        entry.on(incident, after=snapshot(incident, INCIDENT_FIELDS))
        entry.note(
            typed_acknowledgement=True,
            transfer_plan_id=plan.id,
            recommendations=len(recommendations),
            physical_units=sum(row.units for row in recommendations),
            inventory_changed=False,
        )

    upsert_alert(
        db,
        actor,
        alert_type="SURGE_DETECTED",
        severity="CRITICAL",
        title_en=f"Live emergency declared: {run.name}",
        title_ur=f"براہِ راست ہنگامی حالت کا اعلان: {run.name}",
        body_en=(
            f"{len(recommendations)} governed transfer recommendations are ready; "
            "no blood has moved without source approval."
        ),
        body_ur=(
            f"{len(recommendations)} جواب دہ منتقلی سفارشات تیار ہیں؛ ماخذ کی "
            "منظوری کے بغیر کوئی خون منتقل نہیں ہوا۔"
        ),
        organization_id=organization_id,
        payload={
            "incident_id": incident.id,
            "simulation_run_id": run.id,
            "transfer_plan_id": plan.id,
        },
        source_entity_type="emergency_incident",
        source_entity_id=incident.id,
    )
    clear_caches()
    return incident


def active_incidents(db: Session, actor: Actor) -> list[EmergencyIncident]:
    statement = select(EmergencyIncident).where(
        EmergencyIncident.status == "ACTIVE"
    )
    if actor.role not in {
        "SYSTEM_ADMIN",
        "PROVINCIAL_ADMIN",
        "EMERGENCY_CONTROLLER",
    }:
        if not actor.organization_id:
            return []
        statement = statement.where(
            EmergencyIncident.organization_id == actor.organization_id
        )
    return list(
        db.scalars(statement.order_by(EmergencyIncident.declared_at.desc())).all()
    )


def resolve_incident(
    db: Session,
    actor: Actor,
    incident_id: str,
    note: str,
    *,
    now: datetime = DEMO_DATETIME,
) -> EmergencyIncident:
    require(actor, Permission.DECLARE_EMERGENCY, "resolve a live emergency")
    incident = db.get(EmergencyIncident, incident_id)
    if incident is None or (
        actor.role not in {"SYSTEM_ADMIN", "PROVINCIAL_ADMIN", "EMERGENCY_CONTROLLER"}
        and incident.organization_id != actor.organization_id
    ):
        raise ServiceError("INCIDENT_NOT_FOUND", "Incident not found in this scope.")
    if incident.status != "ACTIVE":
        raise ServiceError("INCIDENT_STATE_INVALID", "This incident is not active.")
    note = (note or "").strip()
    if len(note) < 5:
        raise ServiceError(
            "INCIDENT_RESOLUTION_REQUIRED",
            "Record how the live response ended.",
            field="note",
        )
    now = as_utc(now) or DEMO_DATETIME
    before = snapshot(incident, INCIDENT_FIELDS)
    with audited(
        db,
        actor,
        "emergency.resolve",
        "emergency_incident",
        incident.id,
    ) as entry:
        incident.status = "RESOLVED"
        incident.resolved_by = actor.display_name
        incident.resolved_at = now
        incident.resolution_note = note
        entry.on(incident, before=before, after=snapshot(incident, INCIDENT_FIELDS))

    linked_alert = db.scalar(
        select(Alert).where(
            Alert.source_entity_type == "emergency_incident",
            Alert.source_entity_id == incident.id,
            Alert.status.in_(OPEN_STATUSES),
        )
    )
    if linked_alert:
        resolve_alert(
            db,
            actor,
            linked_alert.id,
            "Live incident resolved: " + note,
            now=now,
            automatic=True,
        )
    clear_caches()
    return incident
