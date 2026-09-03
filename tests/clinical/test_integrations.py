"""Sprint 5 ingestion, adapter and provenance safety invariants."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from adapters.fhir import parse_bundle
from adapters.hl7v2 import parse_message
from db.models import (
    ApiClient,
    BloodGroup,
    BloodUnit,
    Component,
    DemandEvent,
    Facility,
    ImportBatch,
    ImportRow,
    SourceProvenance,
    StorageLocation,
)
from services import integration_service
from services.audit import Actor, ServiceError


def _context(engine):
    db = Session(engine)
    component = db.scalar(select(Component).where(Component.code == "PRBC"))
    group = db.scalar(select(BloodGroup).where(BloodGroup.code == "O+"))
    facility = db.scalar(
        select(Facility)
        .join(StorageLocation, StorageLocation.facility_id == Facility.id)
        .where(
            Facility.organization_id.is_not(None),
            Facility.is_active.is_(True),
            StorageLocation.is_active.is_(True),
            StorageLocation.is_quarantine.is_(False),
            StorageLocation.is_out_of_range.is_(False),
            StorageLocation.target_temp_min_c <= component.storage_temp_min_c,
            StorageLocation.target_temp_max_c >= component.storage_temp_max_c,
        )
        .order_by(Facility.code)
    )
    actor = Actor(
        user_id="test:integration-controller",
        display_name="Integration Controller",
        role="SYSTEM_ADMIN",
        facility_id=facility.id,
        organization_id=facility.organization_id,
        organization_wide=True,
    )
    return db, facility, component, group, actor


def _inventory_csv(ref: str, din: str, *, group: str = "O+") -> bytes:
    return (
        "unit_id,din,component_code,blood_group,collected_date,expiry_date,status,screening_status,volume_ml,is_leucodepleted,is_irradiated\n"
        f"{ref},{din},PRBC,{group},2026-08-15T08:00:00+00:00,2026-09-15T08:00:00+00:00,AVAILABLE,PASSED,300,yes,no\n"
    ).encode()


def test_csv_preview_commit_and_repeat_are_idempotent_with_provenance(scratch_database):
    db, facility, _component, _group, actor = _context(scratch_database)
    token = uuid4().hex[:12]
    payload = _inventory_csv(f"SRC-{token}", f"T{token.upper()}")
    before = db.scalar(select(func.count()).select_from(BloodUnit))
    try:
        batch = integration_service.preview_csv(
            db,
            actor,
            organization_id=facility.organization_id,
            facility_id=facility.id,
            data_type="inventory",
            filename="inventory.csv",
            payload=payload,
        )
        assert batch.status == "READY"
        assert batch.valid_rows == 1
        committed = integration_service.commit_batch(
            db,
            actor,
            organization_id=facility.organization_id,
            batch_id=batch.id,
        )
        assert committed.status == "COMMITTED"
        assert db.scalar(select(func.count()).select_from(BloodUnit)) == before + 1
        unit = db.scalar(
            select(BloodUnit).where(BloodUnit.source_system_ref == f"SRC-{token}")
        )
        provenance = db.scalar(
            select(SourceProvenance).where(SourceProvenance.entity_id == unit.id)
        )
        assert provenance.payload_hash
        assert provenance.source_mode == "MANUAL"

        repeated = integration_service.preview_csv(
            db,
            actor,
            organization_id=facility.organization_id,
            facility_id=facility.id,
            data_type="INVENTORY",
            filename="renamed.csv",
            payload=payload,
        )
        assert repeated.id == batch.id
        integration_service.commit_batch(
            db,
            actor,
            organization_id=facility.organization_id,
            batch_id=batch.id,
        )
        assert db.scalar(select(func.count()).select_from(BloodUnit)) == before + 1
    finally:
        db.close()


def test_invalid_rows_never_enter_domain_tables_and_valid_rows_can_commit(scratch_database):
    db, facility, _component, _group, actor = _context(scratch_database)
    token = uuid4().hex[:12]
    payload = (
        "unit_id,din,component_code,blood_group,collected_date,expiry_date,status,screening_status,volume_ml,is_leucodepleted,is_irradiated\n"
        f"GOOD-{token},G{token},PRBC,O+,2026-08-15,2026-09-20,AVAILABLE,PASSED,300,1,0\n"
        f"BAD-{token},B{token},PRBC,X+,2026-09-20,2026-08-15,AVAILABLE,PENDING,300,1,0\n"
    ).encode()
    try:
        batch = integration_service.preview_csv(
            db,
            actor,
            organization_id=facility.organization_id,
            facility_id=facility.id,
            data_type="INVENTORY",
            filename="mixed.csv",
            payload=payload,
        )
        assert batch.status == "NEEDS_REVIEW"
        assert batch.valid_rows == 1
        assert batch.rejected_rows == 1
        bad = db.scalar(
            select(ImportRow).where(
                ImportRow.batch_id == batch.id,
                ImportRow.source_system_ref == f"BAD-{token}",
            )
        )
        assert bad.status == "REJECTED"
        assert {item["code"] for item in bad.errors_json} >= {
            "BLOOD_GROUP_UNKNOWN",
            "EXPIRY_ORDER",
        }
        integration_service.commit_batch(
            db,
            actor,
            organization_id=facility.organization_id,
            batch_id=batch.id,
        )
        assert db.scalar(
            select(BloodUnit).where(BloodUnit.source_system_ref == f"GOOD-{token}")
        )
        assert db.scalar(
            select(BloodUnit).where(BloodUnit.source_system_ref == f"BAD-{token}")
        ) is None
    finally:
        db.close()


def test_demand_plausibility_quarantines_issued_above_requested(scratch_database):
    db, facility, _component, _group, actor = _context(scratch_database)
    token = uuid4().hex[:12]
    payload = (
        "request_id,requested_at,component_code,blood_group,units_requested,units_issued,urgency,clinical_context,outcome\n"
        f"REQ-{token},2026-08-16T10:00:00+00:00,PRBC,O+,2,3,URGENT,TRAUMA,FULFILLED\n"
    ).encode()
    try:
        batch = integration_service.preview_csv(
            db,
            actor,
            organization_id=facility.organization_id,
            facility_id=facility.id,
            data_type="DEMAND",
            filename="demand.csv",
            payload=payload,
        )
        assert batch.quarantined_rows == 1
        with pytest.raises(ServiceError, match="no valid rows"):
            integration_service.commit_batch(
                db,
                actor,
                organization_id=facility.organization_id,
                batch_id=batch.id,
            )
        assert db.scalar(
            select(DemandEvent).where(DemandEvent.source_system_ref == f"REQ-{token}")
        ) is None
    finally:
        db.close()


def test_tenant_boundary_is_checked_inside_import_service(scratch_database):
    db, facility, _component, _group, actor = _context(scratch_database)
    foreign = db.scalar(
        select(Facility).where(
            Facility.organization_id.is_not(None),
            Facility.organization_id != facility.organization_id,
        )
    )
    try:
        with pytest.raises(ServiceError):
            integration_service.preview_csv(
                db,
                actor,
                organization_id=facility.organization_id,
                facility_id=foreign.id,
                data_type="INVENTORY",
                filename="foreign.csv",
                payload=_inventory_csv("FOREIGN", "FOREIGN-DIN"),
            )
    finally:
        db.close()


def test_api_keys_are_hashed_scoped_and_revocable(scratch_database):
    db, facility, _component, _group, actor = _context(scratch_database)
    try:
        client, secret = integration_service.create_api_client(
            db,
            actor,
            organization_id=facility.organization_id,
            name="Test HIS bridge",
            scopes=["facilities:read", "imports:write", "not-a-scope"],
            facility_ids=[facility.id],
        )
        assert secret.startswith("rh_live_")
        assert secret not in client.key_hash
        assert client.scopes_json == ["facilities:read", "imports:write"]
        assert integration_service.authenticate_api_key(db, secret).id == client.id
        integration_service.revoke_api_client(
            db,
            actor,
            organization_id=facility.organization_id,
            client_id=client.id,
        )
        assert integration_service.authenticate_api_key(db, secret) is None
    finally:
        db.close()


def test_fhir_and_hl7_adapters_normalize_to_the_same_contract():
    now = datetime(2026, 8, 16, tzinfo=timezone.utc)
    fhir = parse_bundle(
        {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [
                {
                    "resource": {
                        "resourceType": "BiologicallyDerivedProduct",
                        "identifier": [{"value": "FHIR-U-1"}],
                        "productCode": {"coding": [{"code": "PRBC"}]},
                        "collection": {"collectedDateTime": now.isoformat()},
                        "expirationDate": (now + timedelta(days=42)).isoformat(),
                        "extension": [
                            {"url": "https://rabta.pk/blood-group", "valueCode": "O+"}
                        ],
                    }
                }
            ],
        }
    )
    hl7 = parse_message(
        "MSH|^~\\&|HIS|FAC|RABTA|NET|20260816100000||ORM^O01|1|P|2.5\r"
        "ZRH|HL7-R-1|20260816100000|PRBC|O+|2|1|URGENT|TRAUMA|PARTIAL\r"
    )
    assert fhir.inventory[0]["source_system_ref"] == "FHIR-U-1"
    assert fhir.inventory[0]["component_code"] == "PRBC"
    assert hl7.data_type == "DEMAND"
    assert hl7.rows[0]["component_code"] == "PRBC"
