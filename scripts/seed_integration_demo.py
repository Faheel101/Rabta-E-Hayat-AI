"""Seed a coherent adapter history for every synthetic tenant.

The script is idempotent because it goes through the real ingestion contract:
the deterministic payload checksum resolves to the same batch on every run.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from db.models import Facility, Organization
from db.session import SessionLocal
from services import integration_service
from services.audit import Actor


def _rows(facility: Facility) -> list[dict]:
    prefix = f"SIM-{facility.code}-20260816"
    return [
        {
            "source_system_ref": f"{prefix}-001",
            "requested_at": "2026-08-16T08:15:00+00:00",
            "component_code": "PRBC",
            "blood_group": "O+",
            "units_requested": 2,
            "units_issued": 2,
            "urgency": "URGENT",
            "clinical_context": "TRAUMA",
            "outcome": "FULFILLED",
        },
        {
            "source_system_ref": f"{prefix}-002",
            "requested_at": "2026-08-16T09:20:00+00:00",
            "component_code": "PLT_RD",
            "blood_group": "A+",
            "units_requested": 3,
            "units_issued": 1,
            "urgency": "ROUTINE",
            "clinical_context": "ONCOLOGY",
            "outcome": "PARTIAL",
        },
        {
            "source_system_ref": f"{prefix}-003",
            "requested_at": "2026-08-16T10:05:00+00:00",
            "component_code": "FFP",
            "blood_group": "AB+",
            "units_requested": 1,
            "units_issued": 0,
            "urgency": "EMERGENCY",
            "clinical_context": "OBSTETRIC",
            "outcome": "UNFULFILLED",
        },
        {
            "source_system_ref": f"{prefix}-004",
            "requested_at": "2026-08-16T11:10:00+00:00",
            "component_code": "PRBC",
            "blood_group": "O-",
            "units_requested": 2,
            "units_issued": 4,
            "urgency": "URGENT",
            "clinical_context": "MEDICAL",
            "outcome": "FULFILLED",
        },
    ]


def main() -> int:
    db = SessionLocal()
    created = 0
    try:
        organizations = list(
            db.scalars(
                select(Organization)
                .where(Organization.is_active.is_(True))
                .order_by(Organization.code)
            ).all()
        )
        for organization in organizations:
            facility = db.scalar(
                select(Facility)
                .where(
                    Facility.organization_id == organization.id,
                    Facility.is_active.is_(True),
                )
                .order_by(Facility.code)
            )
            if facility is None:
                continue
            actor = Actor(
                user_id="system:seed-integration-demo",
                display_name="System (synthetic integration feed)",
                role="SYSTEM_ADMIN",
                facility_id=facility.id,
                organization_id=organization.id,
                organization_wide=True,
            )
            rows = _rows(facility)
            batch = integration_service.preview_records(
                db,
                actor,
                organization_id=organization.id,
                facility_id=facility.id,
                data_type="DEMAND",
                mode="SIMULATED",
                filename=f"{facility.code}_20260816_demand.json",
                content_type="application/json",
                raw_payload=json.dumps(rows, sort_keys=True),
                rows=rows,
            )
            if batch.status in {"READY", "NEEDS_REVIEW"}:
                integration_service.commit_batch(
                    db,
                    actor,
                    organization_id=organization.id,
                    batch_id=batch.id,
                )
                created += 1
        print(f"Synthetic integration histories ready for {created} tenants.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
