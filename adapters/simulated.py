"""Production-shaped adapter over the seeded synthetic network."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from adapters.base import AdapterCapabilities, AdapterHealth
from db.models import BloodGroup, BloodUnit, Component, DemandEvent


class SimulatedAdapter:
    mode = "SIMULATED"

    def __init__(self, db: Session, facility_id: str):
        self.db = db
        self.facility_id = facility_id

    def health_check(self) -> AdapterHealth:
        return AdapterHealth(
            status="HEALTHY",
            checked_at=datetime.now(timezone.utc),
            latency_ms=0,
            detail="Seeded synthetic source available",
        )

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            unit_level=True,
            aggregate_only=False,
            supports_push=True,
            max_lookback_days=548,
        )

    def fetch_inventory(self, since: datetime | None = None) -> list[dict]:
        stmt = (
            select(BloodUnit, Component.code, BloodGroup.code)
            .join(Component, Component.id == BloodUnit.component_id)
            .join(BloodGroup, BloodGroup.id == BloodUnit.blood_group_id)
            .where(BloodUnit.facility_id == self.facility_id)
        )
        if since is not None:
            stmt = stmt.where(BloodUnit.last_synced_at >= since)
        return [
            {
                "source_system_ref": unit.source_system_ref or unit.id,
                "din": unit.din,
                "component_code": component,
                "blood_group": group,
                "collected_at": unit.collected_at.isoformat(),
                "expires_at": unit.expires_at.isoformat(),
                "status": unit.status,
                "screening_status": unit.screening_status,
                "volume_ml": unit.volume_ml,
                "is_leucodepleted": unit.is_leucodepleted,
                "is_irradiated": unit.is_irradiated,
            }
            for unit, component, group in self.db.execute(stmt).all()
        ]

    def fetch_demand_events(self, since: datetime | None = None) -> list[dict]:
        stmt = (
            select(DemandEvent, Component.code, BloodGroup.code)
            .join(Component, Component.id == DemandEvent.component_id)
            .join(BloodGroup, BloodGroup.id == DemandEvent.blood_group_id)
            .where(DemandEvent.facility_id == self.facility_id)
        )
        if since is not None:
            stmt = stmt.where(DemandEvent.requested_at >= since)
        return [
            {
                "source_system_ref": event.source_system_ref or event.id,
                "requested_at": event.requested_at.isoformat(),
                "component_code": component,
                "blood_group": group,
                "units_requested": event.units_requested,
                "units_issued": event.units_issued,
                "urgency": event.urgency,
                "clinical_context": event.clinical_context,
                "outcome": event.outcome,
            }
            for event, component, group in self.db.execute(stmt).all()
        ]

    def push_transfer_notice(self, transfer: dict) -> dict:
        return {
            "accepted": True,
            "mode": self.mode,
            "reference": transfer.get("id"),
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
