"""Governed transfer execution and unit-level inventory reconciliation."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from db.models import AuditLog, BloodUnit, Component, Facility, StorageLocation, Transfer
from services import transfer_service as service
from services.audit import Actor, PermissionDenied, ServiceError


def _db(scratch_database):
    return Session(bind=scratch_database, expire_on_commit=False)


def _candidate(db: Session, *, min_units: int = 1, component_code: str = "PRBC"):
    return db.scalars(
        select(Transfer)
        .join(Component, Component.id == Transfer.component_id)
        .where(
            Transfer.status == "RECOMMENDED",
            Transfer.units >= min_units,
            Component.code == component_code,
        )
        .order_by(Transfer.units.desc())
    ).first()


def _actors(db: Session, transfer: Transfer):
    source = db.get(Facility, transfer.from_facility_id)
    destination = db.get(Facility, transfer.to_facility_id)
    return (
        Actor(
            "source-user",
            "Source Coordinator",
            "RBC_COORDINATOR",
            organization_id=source.organization_id,
            organization_wide=True,
        ),
        Actor(
            "destination-user",
            "Destination Coordinator",
            "RBC_COORDINATOR",
            organization_id=destination.organization_id,
            organization_wide=True,
        ),
    )


def _store(db: Session, transfer: Transfer) -> StorageLocation:
    component = db.get(Component, transfer.component_id)
    return db.scalars(
        select(StorageLocation).where(
            StorageLocation.facility_id == transfer.to_facility_id,
            StorageLocation.is_active.is_(True),
            StorageLocation.is_quarantine.is_(False),
            StorageLocation.target_temp_min_c <= component.storage_temp_min_c,
            StorageLocation.target_temp_max_c >= component.storage_temp_max_c,
        )
    ).first()


def _send(db: Session, transfer: Transfer, actor: Actor):
    service.approve_transfer(db, actor, transfer.id)
    service.dispatch_transfer(
        db,
        actor,
        transfer.id,
        custodian="Ayesha Khan",
        courier_name="Rabta Validated Logistics",
        courier_phone="0300-1234567",
        vehicle_ref="LEA-24-7612",
        container_id="VBOX-117",
        seal_number="SEAL-8081",
        departure_temp_c=4.0,
    )
    service.mark_in_transit(db, actor, transfer.id)


def test_complete_lifecycle_moves_each_unit_once_and_audits_every_gate(scratch_database):
    db = _db(scratch_database)
    try:
        transfer = _candidate(db, min_units=2)
        source_actor, destination_actor = _actors(db, transfer)
        manifest = list(transfer.unit_ids)
        _send(db, transfer, source_actor)

        assert {
            db.get(BloodUnit, unit_id).status for unit_id in manifest
        } == {"IN_TRANSIT"}

        service.receive_transfer(
            db,
            destination_actor,
            transfer.id,
            received_unit_ids=manifest,
            accepted_unit_ids=manifest,
            receiving_temp_c=4.7,
            seal_status="INTACT",
            storage_location_id=_store(db, transfer).id,
        )

        assert transfer.status == "RECEIVED"
        assert transfer.receipt_disposition == "COMPLETE"
        units = [db.get(BloodUnit, unit_id) for unit_id in manifest]
        assert {unit.status for unit in units} == {"AVAILABLE"}
        assert {unit.facility_id for unit in units} == {transfer.to_facility_id}
        assert (
            db.scalar(
                select(func.count()).select_from(AuditLog).where(
                    AuditLog.entity_type == "transfer",
                    AuditLog.entity_id == transfer.id,
                )
            )
            == 4
        )

        try:
            service.receive_transfer(
                db,
                destination_actor,
                transfer.id,
                received_unit_ids=manifest,
                accepted_unit_ids=manifest,
                receiving_temp_c=4.7,
                seal_status="INTACT",
                storage_location_id=_store(db, transfer).id,
            )
        except ServiceError as exc:
            assert exc.code == "TRANSFER_STATE_INVALID"
        else:  # pragma: no cover
            raise AssertionError("a receipt must be idempotently refused after completion")
    finally:
        db.close()


def test_temperature_exception_quarantines_every_received_unit(scratch_database):
    db = _db(scratch_database)
    try:
        transfer = _candidate(db, min_units=2)
        source_actor, destination_actor = _actors(db, transfer)
        manifest = list(transfer.unit_ids)
        _send(db, transfer, source_actor)
        service.receive_transfer(
            db,
            destination_actor,
            transfer.id,
            received_unit_ids=manifest,
            accepted_unit_ids=manifest,
            receiving_temp_c=12.0,
            seal_status="INTACT",
            storage_location_id=_store(db, transfer).id,
            discrepancy_note="Data logger showed an excursion after motorway delay.",
        )

        assert transfer.status == "FAILED_COLD_CHAIN"
        assert transfer.failed_reason == "TEMPERATURE_OUT_OF_RANGE"
        assert transfer.accepted_unit_ids == []
        assert set(transfer.quarantined_unit_ids) == set(manifest)
        for unit_id in manifest:
            unit = db.get(BloodUnit, unit_id)
            assert unit.status == "QUARANTINE"
            assert unit.facility_id == transfer.to_facility_id
            assert unit.cold_chain_breach_count >= 1
    finally:
        db.close()


def test_partial_receipt_never_moves_missing_bags_to_destination(scratch_database):
    db = _db(scratch_database)
    try:
        transfer = _candidate(db, min_units=2)
        source_actor, destination_actor = _actors(db, transfer)
        manifest = list(transfer.unit_ids)
        _send(db, transfer, source_actor)
        arrived = manifest[:-1]
        missing = manifest[-1]
        service.receive_transfer(
            db,
            destination_actor,
            transfer.id,
            received_unit_ids=arrived,
            accepted_unit_ids=arrived,
            receiving_temp_c=4.5,
            seal_status="INTACT",
            storage_location_id=_store(db, transfer).id,
            discrepancy_note="One manifest label was not present in the sealed box.",
        )

        assert transfer.status == "RECEIVED"
        assert transfer.receipt_disposition == "PARTIAL"
        assert transfer.missing_unit_ids == [missing]
        assert db.get(BloodUnit, missing).status == "MISSING_IN_TRANSIT"
        assert db.get(BloodUnit, missing).facility_id == transfer.from_facility_id
        assert {
            db.get(BloodUnit, unit_id).facility_id for unit_id in arrived
        } == {transfer.to_facility_id}
    finally:
        db.close()


def test_cancellation_releases_reserved_inventory(scratch_database):
    db = _db(scratch_database)
    try:
        transfer = _candidate(db)
        source_actor, _ = _actors(db, transfer)
        manifest = list(transfer.unit_ids)
        service.approve_transfer(db, source_actor, transfer.id)
        assert {db.get(BloodUnit, unit_id).status for unit_id in manifest} == {"RESERVED"}
        service.cancel_transfer(
            db,
            source_actor,
            transfer.id,
            "Validated cold box failed pre-departure inspection.",
        )
        assert transfer.status == "CANCELLED"
        assert {db.get(BloodUnit, unit_id).status for unit_id in manifest} == {"AVAILABLE"}
    finally:
        db.close()


def test_service_enforces_permission_tenant_and_reserve_floor(scratch_database):
    db = _db(scratch_database)
    try:
        transfer = _candidate(db)
        source_actor, _ = _actors(db, transfer)
        low_privilege = Actor(
            "collector",
            "Collector",
            "PHLEBOTOMIST",
            organization_id=source_actor.organization_id,
            organization_wide=True,
        )
        try:
            service.approve_transfer(db, low_privilege, transfer.id)
        except PermissionDenied:
            pass
        else:  # pragma: no cover
            raise AssertionError("a phlebotomist must not approve a transfer")

        other_org = db.scalar(
            select(Facility.organization_id).where(
                Facility.organization_id.is_not(None),
                Facility.organization_id != source_actor.organization_id,
            )
        )
        foreign_actor = Actor(
            "foreign",
            "Foreign Coordinator",
            "RBC_COORDINATOR",
            organization_id=other_org,
            organization_wide=True,
        )
        try:
            service.approve_transfer(db, foreign_actor, transfer.id)
        except ServiceError as exc:
            assert exc.code == "TRANSFER_NOT_FOUND"
        else:  # pragma: no cover
            raise AssertionError("another tenant must not operate the transfer")

        source = db.get(Facility, transfer.from_facility_id)
        component = db.get(Component, transfer.component_id)
        unit = db.get(BloodUnit, transfer.unit_ids[0])
        from db.models import BloodGroup

        group = db.get(BloodGroup, unit.blood_group_id)
        original = source.min_reserve_policy_json
        source.min_reserve_policy_json = {
            **original,
            component.code: {**original.get(component.code, {}), group.code: 999999},
        }
        try:
            service.approve_transfer(db, source_actor, transfer.id)
        except ServiceError as exc:
            assert exc.code == "RESERVE_FLOOR_BREACH"
        else:  # pragma: no cover
            raise AssertionError("approval must revalidate the live reserve floor")
        db.rollback()
    finally:
        db.close()


def test_rejection_requires_structured_training_reason(scratch_database):
    db = _db(scratch_database)
    try:
        transfer = _candidate(db)
        source_actor, _ = _actors(db, transfer)
        try:
            service.reject_transfer(db, source_actor, transfer.id, "free_text_only")
        except ServiceError as exc:
            assert exc.code == "REJECTION_REASON_INVALID"
        else:  # pragma: no cover
            raise AssertionError("free-text-only rejection must not be stored")

        service.reject_transfer(
            db,
            source_actor,
            transfer.id,
            "no_transport",
            "Validated carrier unavailable before the safe dispatch deadline.",
        )
        assert transfer.status == "REJECTED"
        assert transfer.rejection_reason == "no_transport"
        audit = db.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == transfer.id,
                AuditLog.action == "transfer.reject",
            )
        ).one()
        assert audit.after_json["_context"]["reason"] == "no_transport"
    finally:
        db.close()


def test_cross_tenant_approval_requires_live_network_sharing_consent(scratch_database):
    db = _db(scratch_database)
    try:
        Source = aliased(Facility)
        Destination = aliased(Facility)
        transfer = db.scalars(
            select(Transfer)
            .join(Source, Source.id == Transfer.from_facility_id)
            .join(Destination, Destination.id == Transfer.to_facility_id)
            .where(
                Transfer.status == "RECOMMENDED",
                Source.organization_id != Destination.organization_id,
            )
        ).first()
        source_actor, _ = _actors(db, transfer)
        destination = db.get(Facility, transfer.to_facility_id)
        destination.shares_inventory = False

        try:
            service.approve_transfer(db, source_actor, transfer.id)
        except ServiceError as exc:
            assert exc.code == "NETWORK_SHARING_NOT_AUTHORIZED"
        else:  # pragma: no cover
            raise AssertionError("revoked facility sharing consent must veto approval")
        db.rollback()
    finally:
        db.close()
