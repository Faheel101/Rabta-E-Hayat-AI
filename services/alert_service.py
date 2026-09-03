"""Governed alert lifecycle with deduplication, cooldown and durable delivery.

An alert is operational state, not a toast. Evidence updates one open record,
acknowledgement establishes ownership, resolution records why it closed, and
every transition shares a transaction with its audit entry. External channels
write to an outbox so a missing SMS/email provider never loses the in-app alert.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.auth import Permission
from core import config
from core.clock import as_utc
from db.models import (
    Alert,
    AlertDelivery,
    BloodGroup,
    Component,
    ExpiryRescue,
    Facility,
    MartDaysOfCover,
    MartFacilityKpi,
    Transfer,
    new_id,
)
from services.audit import Actor, ServiceError, audited, require, snapshot
from services.common import DEMO_DATETIME, clear_caches

SEVERITY_ORDER = {"MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
OPEN_STATUSES = {"OPEN", "ACKNOWLEDGED"}
MANAGED_TYPES = {
    "SHORTAGE_PREDICTED",
    "RESERVE_BREACHED",
    "EXPIRY_RISK",
    "TRANSFER_RECOMMENDED",
    "TRANSFER_OVERDUE",
    "COLD_CHAIN_BREACH",
    "DATA_FEED_STALE",
}
COOLDOWN_HOURS = float(config.get("alerts.cooldown_hours", 6))
ESCALATION_MINUTES = int(config.get("alerts.critical_escalation_minutes", 60))
OVERDUE_MINUTES = int(config.get("alerts.transfer_overdue_minutes", 120))
STALE_HOURS = float(config.get("alerts.stale_feed_hours", 36))

ALERT_FIELDS = (
    "alert_type",
    "severity",
    "status",
    "title_en",
    "title_ur",
    "body_en",
    "body_ur",
    "payload_json",
    "occurrence_count",
    "last_notified_at",
    "acknowledged_by",
    "acknowledged_at",
    "acknowledgement_note",
    "assigned_to",
    "resolved_at",
    "resolved_by",
    "resolution_reason",
    "escalated_at",
    "escalated_to",
)


def build_dedup_key(
    *,
    alert_type: str,
    facility_id: str | None,
    organization_id: str | None,
    component_id: int | None = None,
    blood_group_id: int | None = None,
) -> str:
    owner = facility_id or f"org:{organization_id or 'network'}"
    return ":".join(
        [owner, alert_type, str(component_id or "-"), str(blood_group_id or "-")]
    )


def _channels(severity: str) -> list[str]:
    configured = (config.get("alerts.channels") or {}).get(severity, ["IN_APP"])
    result = [str(value).upper() for value in configured]
    return list(dict.fromkeys(["IN_APP", *result]))


def _queue_deliveries(
    db: Session,
    alert: Alert,
    *,
    now: datetime,
    reason: str,
) -> None:
    for channel in _channels(alert.severity):
        in_app = channel == "IN_APP"
        db.add(
            AlertDelivery(
                id=new_id(),
                alert_id=alert.id,
                channel=channel,
                recipient=None,
                status="DELIVERED" if in_app else "QUEUED",
                delivered_at=now if in_app else None,
                payload_json={"reason": reason, "severity": alert.severity},
            )
        )


def upsert_alert(
    db: Session,
    actor: Actor,
    *,
    alert_type: str,
    severity: str,
    title_en: str,
    title_ur: str,
    body_en: str,
    body_ur: str,
    facility_id: str | None = None,
    organization_id: str | None = None,
    component_id: int | None = None,
    blood_group_id: int | None = None,
    payload: dict | None = None,
    source_entity_type: str | None = None,
    source_entity_id: str | None = None,
    now: datetime | None = None,
) -> Alert:
    """Create one open alert or update its evidence without notification spam."""

    severity = str(severity).upper()
    if severity not in SEVERITY_ORDER:
        raise ServiceError("ALERT_SEVERITY_INVALID", "Alert severity is invalid.")
    if alert_type not in MANAGED_TYPES | {"SURGE_DETECTED"}:
        raise ServiceError("ALERT_TYPE_INVALID", "Alert type is invalid.")
    if facility_id:
        facility = db.get(Facility, facility_id)
        if facility is None:
            raise ServiceError("FACILITY_NOT_FOUND", "Alert facility not found.")
        organization_id = organization_id or facility.organization_id

    now = as_utc(now) or datetime.now(timezone.utc)
    key = build_dedup_key(
        alert_type=alert_type,
        facility_id=facility_id,
        organization_id=organization_id,
        component_id=component_id,
        blood_group_id=blood_group_id,
    )
    alert = db.scalar(
        select(Alert)
        .where(Alert.dedup_key == key, Alert.status.in_(OPEN_STATUSES))
        .order_by(Alert.created_at.desc())
    )

    if alert is None:
        alert = Alert(
            id=new_id(),
            facility_id=facility_id,
            organization_id=organization_id,
            component_id=component_id,
            blood_group_id=blood_group_id,
            alert_type=alert_type,
            severity=severity,
            status="OPEN",
            title_en=title_en,
            title_ur=title_ur,
            body_en=body_en,
            body_ur=body_ur,
            payload_json=payload or {},
            source_entity_type=source_entity_type,
            source_entity_id=source_entity_id,
            dedup_key=key,
            occurrence_count=1,
            created_at=now,
            updated_at=now,
            last_notified_at=now,
        )
        with audited(db, actor, "alert.open", "alert", alert.id) as entry:
            db.add(alert)
            _queue_deliveries(db, alert, now=now, reason="new_alert")
            entry.on(alert, after=snapshot(alert, ALERT_FIELDS))
        clear_caches()
        return alert

    before = snapshot(alert, ALERT_FIELDS)
    previous_severity = alert.severity
    escalated = SEVERITY_ORDER[severity] > SEVERITY_ORDER.get(previous_severity, 0)
    last_notified = as_utc(alert.last_notified_at)
    cooldown_elapsed = (
        last_notified is None or now - last_notified >= timedelta(hours=COOLDOWN_HOURS)
    )
    should_notify = escalated or cooldown_elapsed

    with audited(db, actor, "alert.update", "alert", alert.id) as entry:
        if escalated:
            alert.severity = severity
        alert.title_en = title_en
        alert.title_ur = title_ur
        alert.body_en = body_en
        alert.body_ur = body_ur
        alert.payload_json = payload or {}
        alert.source_entity_type = source_entity_type or alert.source_entity_type
        alert.source_entity_id = source_entity_id or alert.source_entity_id
        alert.occurrence_count = int(alert.occurrence_count or 0) + 1
        alert.updated_at = now
        if should_notify:
            alert.last_notified_at = now
            _queue_deliveries(
                db,
                alert,
                now=now,
                reason="severity_escalated" if escalated else "cooldown_elapsed",
            )
        entry.on(alert, before=before, after=snapshot(alert, ALERT_FIELDS))
        entry.note(notification_queued=should_notify, dedup_key=key)
    clear_caches()
    return alert


def _visible(alert: Alert, actor: Actor) -> bool:
    if actor.role in {"SYSTEM_ADMIN", "PROVINCIAL_ADMIN", "EMERGENCY_CONTROLLER"}:
        return True
    if actor.organization_id and alert.organization_id == actor.organization_id:
        return True
    return bool(actor.facility_id and alert.facility_id == actor.facility_id)


def _get_visible(db: Session, actor: Actor, alert_id: str) -> Alert:
    alert = db.get(Alert, alert_id)
    if alert is None or not _visible(alert, actor):
        raise ServiceError("ALERT_NOT_FOUND", "Alert not found in this scope.")
    return alert


def acknowledge_alert(
    db: Session,
    actor: Actor,
    alert_id: str,
    note: str,
    *,
    now: datetime | None = None,
) -> Alert:
    require(actor, Permission.ACKNOWLEDGE_ALERT, "acknowledge alerts")
    alert = _get_visible(db, actor, alert_id)
    if alert.status != "OPEN":
        raise ServiceError("ALERT_STATE_INVALID", "Only an open alert can be acknowledged.")
    note = (note or "").strip()
    if len(note) < 3:
        raise ServiceError("ACK_NOTE_REQUIRED", "Add a short acknowledgement note.", field="note")
    now = as_utc(now) or datetime.now(timezone.utc)
    before = snapshot(alert, ALERT_FIELDS)
    with audited(db, actor, "alert.acknowledge", "alert", alert.id) as entry:
        alert.status = "ACKNOWLEDGED"
        alert.acknowledged_by = actor.display_name
        alert.acknowledged_at = now
        alert.acknowledgement_note = note
        alert.assigned_to = actor.display_name
        alert.updated_at = now
        entry.on(alert, before=before, after=snapshot(alert, ALERT_FIELDS))
    clear_caches()
    return alert


def resolve_alert(
    db: Session,
    actor: Actor,
    alert_id: str,
    reason: str,
    *,
    now: datetime | None = None,
    automatic: bool = False,
) -> Alert:
    if not automatic:
        require(actor, Permission.RESOLVE_ALERT, "resolve alerts")
    alert = _get_visible(db, actor, alert_id) if not automatic else db.get(Alert, alert_id)
    if alert is None:
        raise ServiceError("ALERT_NOT_FOUND", "Alert not found.")
    if alert.status not in OPEN_STATUSES:
        raise ServiceError("ALERT_STATE_INVALID", "This alert is already resolved.")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ServiceError("RESOLUTION_REQUIRED", "Record why the alert is resolved.", field="reason")
    now = as_utc(now) or datetime.now(timezone.utc)
    before = snapshot(alert, ALERT_FIELDS)
    action = "alert.auto_resolve" if automatic else "alert.resolve"
    with audited(db, actor, action, "alert", alert.id) as entry:
        alert.status = "RESOLVED"
        alert.resolved_by = actor.display_name
        alert.resolved_at = now
        alert.resolution_reason = reason
        alert.updated_at = now
        entry.on(alert, before=before, after=snapshot(alert, ALERT_FIELDS))
    clear_caches()
    return alert


def escalate_unacknowledged(
    db: Session,
    actor: Actor,
    *,
    now: datetime | None = None,
) -> int:
    now = as_utc(now) or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=ESCALATION_MINUTES)
    rows = list(
        db.scalars(
            select(Alert).where(
                Alert.status == "OPEN",
                Alert.severity == "CRITICAL",
                Alert.created_at <= cutoff,
                Alert.escalated_at.is_(None),
            )
        ).all()
    )
    for alert in rows:
        before = snapshot(alert, ALERT_FIELDS)
        with audited(db, actor, "alert.escalate", "alert", alert.id) as entry:
            alert.escalated_at = now
            alert.escalated_to = "Parent RBC coordinator"
            alert.updated_at = now
            _queue_deliveries(db, alert, now=now, reason="critical_unacknowledged")
            entry.on(alert, before=before, after=snapshot(alert, ALERT_FIELDS))
    return len(rows)


def alert_workspace(
    db: Session,
    *,
    organization_id: str,
    facility_ids: list[str],
    status_filter: str | None = None,
    severity_filter: str | None = None,
    type_filter: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    FacilityAlias = aliased(Facility)
    ComponentAlias = aliased(Component)
    GroupAlias = aliased(BloodGroup)
    visible = or_(
        Alert.organization_id == organization_id,
        Alert.facility_id.in_(facility_ids or ["__none__"]),
    )
    statement = (
        select(
            Alert,
            FacilityAlias.name_en.label("facility_name"),
            ComponentAlias.code.label("component_code"),
            GroupAlias.code.label("group_code"),
        )
        .outerjoin(FacilityAlias, FacilityAlias.id == Alert.facility_id)
        .outerjoin(ComponentAlias, ComponentAlias.id == Alert.component_id)
        .outerjoin(GroupAlias, GroupAlias.id == Alert.blood_group_id)
        .where(visible)
    )
    if status_filter in {"OPEN", "ACKNOWLEDGED", "RESOLVED"}:
        statement = statement.where(Alert.status == status_filter)
    if severity_filter in SEVERITY_ORDER:
        statement = statement.where(Alert.severity == severity_filter)
    if type_filter in MANAGED_TYPES | {"SURGE_DETECTED"}:
        statement = statement.where(Alert.alert_type == type_filter)
    total = int(
        db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    )
    page_size = max(10, min(100, int(page_size)))
    pages = max(1, math.ceil(total / page_size))
    page = max(1, min(int(page), pages))
    rows = []
    for result in db.execute(
        statement.order_by(Alert.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all():
        row = dict(result._mapping)
        row["record"] = row.pop("Alert")
        rows.append(row)
    counts = {
        status: int(
            db.scalar(
                select(func.count()).select_from(Alert).where(visible, Alert.status == status)
            )
            or 0
        )
        for status in ("OPEN", "ACKNOWLEDGED", "RESOLVED")
    }
    critical = int(
        db.scalar(
            select(func.count()).select_from(Alert).where(
                visible, Alert.status.in_(OPEN_STATUSES), Alert.severity == "CRITICAL"
            )
        )
        or 0
    )
    return {
        "rows": rows,
        "counts": counts,
        "critical": critical,
        "total": total,
        "page": page,
        "pages": pages,
    }


def open_alert_count(db: Session, organization_id: str, facility_ids: list[str]) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(Alert).where(
                or_(
                    Alert.organization_id == organization_id,
                    Alert.facility_id.in_(facility_ids or ["__none__"]),
                ),
                Alert.status.in_(OPEN_STATUSES),
            )
        )
        or 0
    )


def _evidence(db: Session, facility_ids: list[str]) -> list[dict]:
    facilities = {
        item.id: item
        for item in db.scalars(select(Facility).where(Facility.id.in_(facility_ids))).all()
    }
    components = {item.id: item for item in db.scalars(select(Component)).all()}
    groups = {item.id: item for item in db.scalars(select(BloodGroup)).all()}
    evidence: list[dict] = []

    positions = list(
        db.scalars(
            select(MartDaysOfCover).where(MartDaysOfCover.facility_id.in_(facility_ids))
        ).all()
    )
    for row in positions:
        facility = facilities.get(row.facility_id)
        component = components.get(row.component_id)
        group = groups.get(row.blood_group_id)
        if not facility or not component or not group:
            continue
        if float(row.shortage_probability or 0) >= 0.6:
            severity = "CRITICAL" if row.risk_bucket == "CRITICAL" else "HIGH"
            evidence.append(
                dict(
                    alert_type="SHORTAGE_PREDICTED",
                    severity=severity,
                    facility_id=facility.id,
                    organization_id=facility.organization_id,
                    component_id=component.id,
                    blood_group_id=group.id,
                    title_en=f"{group.code} {component.code} shortage predicted",
                    title_ur=f"{group.code} {component.code} کی قلت کا امکان",
                    body_en=f"{facility.name_en} has {row.units_available} units and a {float(row.shortage_probability):.0%} shortage probability.",
                    body_ur=f"{facility.name_en} میں {row.units_available} یونٹس ہیں اور قلت کا امکان {float(row.shortage_probability):.0%} ہے۔",
                    payload={"units_available": row.units_available, "shortage_probability": row.shortage_probability, "risk_bucket": row.risk_bucket},
                    source_entity_type="mart_days_of_cover",
                    source_entity_id=str(row.id),
                )
            )
        if float(row.units_available or 0) < float(row.reserve_floor or 0):
            evidence.append(
                dict(
                    alert_type="RESERVE_BREACHED",
                    severity="CRITICAL",
                    facility_id=facility.id,
                    organization_id=facility.organization_id,
                    component_id=component.id,
                    blood_group_id=group.id,
                    title_en=f"{group.code} {component.code} reserve breached",
                    title_ur=f"{group.code} {component.code} محفوظ حد سے کم",
                    body_en=f"{facility.name_en} holds {row.units_available} units against a reserve floor of {math.ceil(row.reserve_floor)}.",
                    body_ur=f"{facility.name_en} میں {row.units_available} یونٹس ہیں جبکہ محفوظ حد {math.ceil(row.reserve_floor)} ہے۔",
                    payload={"units_available": row.units_available, "reserve_floor": row.reserve_floor},
                    source_entity_type="mart_days_of_cover",
                    source_entity_id=str(row.id),
                )
            )

    rescue_groups = defaultdict(list)
    for row in db.scalars(
        select(ExpiryRescue).where(
            ExpiryRescue.facility_id.in_(facility_ids),
            ExpiryRescue.waste_probability >= 0.6,
            ExpiryRescue.transferable.is_(True),
        )
    ).all():
        rescue_groups[(row.facility_id, row.component_id, row.blood_group_id)].append(row)
    for (facility_id, component_id, group_id), rows in rescue_groups.items():
        facility, component, group = facilities.get(facility_id), components.get(component_id), groups.get(group_id)
        if not facility or not component or not group:
            continue
        severity = "HIGH" if any(row.rescue_tier == "ACT_NOW" for row in rows) else "MEDIUM"
        evidence.append(
            dict(
                alert_type="EXPIRY_RISK", severity=severity,
                facility_id=facility_id, organization_id=facility.organization_id,
                component_id=component_id, blood_group_id=group_id,
                title_en=f"{len(rows)} {group.code} {component.code} units at expiry risk",
                title_ur=f"{group.code} {component.code} کے {len(rows)} یونٹس انقضا کے خطرے میں",
                body_en="A feasible rescue route exists; review before the dispatch deadline.",
                body_ur="بچاؤ کا قابلِ عمل راستہ موجود ہے؛ روانگی کی آخری مہلت سے پہلے جائزہ لیں۔",
                payload={"units": len(rows), "tier": severity},
                source_entity_type="expiry_rescue", source_entity_id=rows[0].id,
            )
        )

    transfer_groups = defaultdict(list)
    transfers = db.scalars(
        select(Transfer).where(
            or_(Transfer.from_facility_id.in_(facility_ids), Transfer.to_facility_id.in_(facility_ids)),
            Transfer.status.in_(("RECOMMENDED", "APPROVED", "FAILED_COLD_CHAIN")),
        )
    ).all()
    for row in transfers:
        if row.status == "RECOMMENDED" and row.from_facility_id in facilities:
            transfer_groups[(row.from_facility_id, row.component_id, row.blood_group_id)].append(row)
        elif row.status == "APPROVED" and as_utc(row.approved_at) and as_utc(row.approved_at) <= DEMO_DATETIME - timedelta(minutes=OVERDUE_MINUTES):
            facility = facilities.get(row.from_facility_id)
            if facility:
                evidence.append(dict(alert_type="TRANSFER_OVERDUE", severity="HIGH", facility_id=facility.id, organization_id=facility.organization_id, component_id=row.component_id, blood_group_id=row.blood_group_id, title_en="Approved transfer is overdue", title_ur="منظور شدہ منتقلی میں تاخیر", body_en=f"Transfer {row.tracking_code or row.id[:8]} has not been dispatched within the SLA.", body_ur=f"منتقلی {row.tracking_code or row.id[:8]} مقررہ وقت میں روانہ نہیں ہوئی۔", payload={"transfer_id": row.id}, source_entity_type="transfer", source_entity_id=row.id))
        elif row.status == "FAILED_COLD_CHAIN" and row.to_facility_id in facilities:
            facility = facilities[row.to_facility_id]
            evidence.append(dict(alert_type="COLD_CHAIN_BREACH", severity="CRITICAL", facility_id=facility.id, organization_id=facility.organization_id, component_id=row.component_id, blood_group_id=row.blood_group_id, title_en="Transfer cold-chain breach", title_ur="منتقلی میں کولڈ چین کی خلاف ورزی", body_en=f"Shipment {row.tracking_code or row.id[:8]} was quarantined on receipt.", body_ur=f"شپمنٹ {row.tracking_code or row.id[:8]} وصولی پر قرنطینہ کر دی گئی۔", payload={"transfer_id": row.id, "failed_reason": row.failed_reason}, source_entity_type="transfer", source_entity_id=row.id))
    for (facility_id, component_id, group_id), rows in transfer_groups.items():
        facility = facilities[facility_id]
        evidence.append(dict(alert_type="TRANSFER_RECOMMENDED", severity="MEDIUM", facility_id=facility_id, organization_id=facility.organization_id, component_id=component_id, blood_group_id=group_id, title_en=f"{len(rows)} transfer recommendations await review", title_ur=f"{len(rows)} منتقلی سفارشات جائزے کی منتظر", body_en=f"{sum(row.units for row in rows)} units require an accountable outbound decision.", body_ur=f"{sum(row.units for row in rows)} یونٹس کے لیے جواب دہ فیصلہ درکار ہے۔", payload={"transfers": len(rows), "units": sum(row.units for row in rows)}, source_entity_type="transfer_plan", source_entity_id=rows[0].plan_id))

    for row in db.scalars(select(MartFacilityKpi).where(MartFacilityKpi.facility_id.in_(facility_ids))).all():
        if row.feed_status != "HEALTHY" or float(row.feed_age_hours or 0) >= STALE_HOURS:
            facility = facilities.get(row.facility_id)
            if facility:
                evidence.append(dict(alert_type="DATA_FEED_STALE", severity="MEDIUM", facility_id=facility.id, organization_id=facility.organization_id, title_en="Facility data feed is stale", title_ur="مرکز کا ڈیٹا پرانا ہے", body_en=f"{facility.name_en} last reported {float(row.feed_age_hours or 0):.0f} hours ago.", body_ur=f"{facility.name_en} نے آخری ڈیٹا {float(row.feed_age_hours or 0):.0f} گھنٹے پہلے بھیجا۔", payload={"feed_age_hours": row.feed_age_hours, "feed_status": row.feed_status}, source_entity_type="mart_facility_kpi", source_entity_id=str(row.id)))
    return evidence


def sync_operational_alerts(
    db: Session,
    actor: Actor,
    facility_ids: list[str],
    *,
    now: datetime | None = None,
) -> dict:
    """Refresh evidence and auto-resolve managed alerts whose condition cleared."""

    now = as_utc(now) or datetime.now(timezone.utc)
    evidence = _evidence(db, facility_ids)
    active_keys = set()
    opened_or_updated = 0
    for item in evidence:
        active_keys.add(build_dedup_key(alert_type=item["alert_type"], facility_id=item.get("facility_id"), organization_id=item.get("organization_id"), component_id=item.get("component_id"), blood_group_id=item.get("blood_group_id")))
        upsert_alert(db, actor, now=now, **item)
        opened_or_updated += 1
    stale = list(
        db.scalars(
            select(Alert).where(
                Alert.facility_id.in_(facility_ids),
                Alert.alert_type.in_(MANAGED_TYPES),
                Alert.status.in_(OPEN_STATUSES),
                Alert.dedup_key.not_in(active_keys or {"__none__"}),
            )
        ).all()
    )
    for alert in stale:
        resolve_alert(
            db,
            actor,
            alert.id,
            "Underlying condition cleared during alert synchronization.",
            now=now,
            automatic=True,
        )
    escalated = escalate_unacknowledged(db, actor, now=now)
    return {"active_evidence": opened_or_updated, "auto_resolved": len(stale), "escalated": escalated}
