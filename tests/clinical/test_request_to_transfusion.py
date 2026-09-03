"""Safety invariants for the request-to-transfusion state machine."""

from __future__ import annotations

from datetime import timedelta
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.clock import DEMO_DATETIME
from db.base import Base
from db.models import (
    AuditLog,
    BloodGroup,
    BloodRequest,
    BloodUnit,
    Compatibility,
    Component,
    DemandEvent,
    Facility,
    IntelligenceRefreshState,
    Organization,
    TransfusionRecord,
    UnitIssue,
)
from services import request_service, traceability
from services.audit import Actor, PermissionDenied, ServiceError


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    session = maker()

    organization = Organization(
        id="org-one",
        code="ORG1",
        name_en="Test Hospital Group",
        org_type="HOSPITAL_GROUP",
    )
    other_organization = Organization(
        id="org-two",
        code="ORG2",
        name_en="Other Hospital",
    )
    facility = Facility(
        id="facility-one",
        code="FAC1",
        organization_id=organization.id,
        name_en="Test Blood Bank",
        district="Lahore",
    )
    other_facility = Facility(
        id="facility-two",
        code="FAC2",
        organization_id=other_organization.id,
        name_en="Other Blood Bank",
        district="Lahore",
    )
    a_pos = BloodGroup(id=1, code="A+", abo="A", rh="+")
    o_pos = BloodGroup(id=2, code="O+", abo="O", rh="+")
    b_pos = BloodGroup(id=3, code="B+", abo="B", rh="+")
    o_neg = BloodGroup(id=4, code="O-", abo="O", rh="-")
    prbc = Component(
        id=1,
        code="PRBC",
        name_en="Packed red blood cells",
        shelf_life_days=42,
        storage_temp_min_c=2,
        storage_temp_max_c=6,
    )
    session.add_all(
        [
            organization,
            other_organization,
            facility,
            other_facility,
            a_pos,
            o_pos,
            b_pos,
            o_neg,
            prbc,
            Compatibility(
                component_id=prbc.id,
                recipient_group_id=a_pos.id,
                donor_group_id=a_pos.id,
                is_compatible=True,
                preference_rank=1,
            ),
            Compatibility(
                component_id=prbc.id,
                recipient_group_id=a_pos.id,
                donor_group_id=o_pos.id,
                is_compatible=True,
                preference_rank=2,
            ),
        ]
    )
    session.commit()

    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def officer():
    return Actor(
        user_id="officer-one",
        display_name="Test Blood Bank Officer",
        role="BLOOD_BANK_OFFICER",
        facility_id="facility-one",
        organization_id="org-one",
    )


def add_unit(db, *, unit_id: str, group_id: int, expires_in_days: int = 10, facility_id="facility-one"):
    unit = BloodUnit(
        id=unit_id,
        din=f"ZAA26{uuid.uuid5(uuid.NAMESPACE_DNS, unit_id).hex[:8].upper()}",
        facility_id=facility_id,
        component_id=1,
        blood_group_id=group_id,
        volume_ml=350,
        collected_at=DEMO_DATETIME - timedelta(days=5),
        expires_at=DEMO_DATETIME + timedelta(days=expires_in_days),
        status="AVAILABLE",
        screening_status="PASSED",
    )
    db.add(unit)
    db.commit()
    return unit


def make_request(db, officer, *, units=1):
    return request_service.create_request(
        db,
        officer,
        patient_ref="EP-LHR-0001",
        patient_age_years=34,
        patient_sex="FEMALE",
        patient_blood_group_id=1,
        component_id=1,
        units_requested=units,
        urgency="URGENT",
        clinical_context="OBSTETRIC",
        ward="Labour ward",
        requested_by="Dr Test",
        required_by=DEMO_DATETIME + timedelta(hours=2),
    )


def crossmatch_and_issue(db, officer, request, unit):
    request_service.record_crossmatch(
        db,
        officer,
        request_id=request.id,
        unit_id=unit.id,
        result="COMPATIBLE",
        method="GEL_CARD",
    )
    return request_service.issue_unit(
        db,
        officer,
        request_id=request.id,
        unit_id=unit.id,
        collected_by="Nurse Test",
        patient_ref_confirmation=request.patient_ref,
        destination_ward="Labour ward",
    )


def test_request_creation_is_scoped_and_audited(db, officer):
    request = make_request(db, officer)

    assert request.facility_id == officer.facility_id
    assert request.status == "PENDING"
    assert request.request_code.startswith("BR-260806-")

    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.action == "BLOOD_REQUEST_CREATED",
            AuditLog.entity_id == request.id,
        )
    )
    assert audit is not None
    assert audit.after_json["patient_ref"] == "EP-LHR-0001"

    demand = db.scalar(
        select(DemandEvent).where(DemandEvent.blood_request_id == request.id)
    )
    assert demand is not None
    assert demand.source_system_ref == f"clinical-request:{request.id}"
    assert demand.facility_id == request.facility_id
    assert demand.blood_group_id == request.patient_blood_group_id
    assert demand.units_requested == 1
    assert demand.units_issued == 0
    assert demand.outcome == "UNFULFILLED"

    refresh = db.get(IntelligenceRefreshState, "decision-intelligence")
    assert refresh.status == "DIRTY"
    assert refresh.source_version == 1
    assert refresh.completed_version == 0


def test_unknown_group_is_not_invented_and_later_update_is_idempotent(db, officer):
    request = request_service.create_request(
        db,
        officer,
        patient_ref="EP-UNKNOWN-0001",
        component_id=1,
        units_requested=2,
        urgency="EMERGENCY",
        clinical_context="TRAUMA",
    )
    assert db.scalar(
        select(DemandEvent).where(DemandEvent.blood_request_id == request.id)
    ) is None

    for urgency in ("EMERGENCY", "URGENT"):
        request_service.update_request(
            db,
            officer,
            request_id=request.id,
            patient_ref=request.patient_ref,
            patient_blood_group_id=1,
            component_id=1,
            units_requested=2,
            urgency=urgency,
            clinical_context="TRAUMA",
        )

    events = list(
        db.scalars(
            select(DemandEvent).where(DemandEvent.blood_request_id == request.id)
        ).all()
    )
    assert len(events) == 1
    assert events[0].urgency == "URGENT"


def test_candidates_are_compatibility_ranked_then_fefo(db, officer):
    request = make_request(db, officer)
    identical_later = add_unit(db, unit_id="unit-a-later", group_id=1, expires_in_days=8)
    identical_first = add_unit(db, unit_id="unit-a-first", group_id=1, expires_in_days=4)
    compatible_earliest = add_unit(db, unit_id="unit-o-first", group_id=2, expires_in_days=2)
    add_unit(db, unit_id="unit-b-no", group_id=3, expires_in_days=1)

    rows = request_service.candidate_units(db, officer, request_id=request.id)

    assert [row.id for row in rows] == [
        identical_first.id,
        identical_later.id,
        compatible_earliest.id,
    ]


def test_incompatible_group_cannot_be_recorded_as_compatible(db, officer):
    request = make_request(db, officer)
    unit = add_unit(db, unit_id="unit-b-incompatible", group_id=3)

    with pytest.raises(ServiceError) as raised:
        request_service.record_crossmatch(
            db,
            officer,
            request_id=request.id,
            unit_id=unit.id,
            result="COMPATIBLE",
            method="AHG_COOMBS",
        )

    assert raised.value.code == "ABO_RH_INCOMPATIBLE"
    db.refresh(unit)
    assert unit.status == "AVAILABLE"


def test_full_chain_closes_request_and_preserves_custody(db, officer):
    request = make_request(db, officer)
    unit = add_unit(db, unit_id="unit-full-chain", group_id=1)
    issue = crossmatch_and_issue(db, officer, request, unit)

    db.refresh(request)
    db.refresh(unit)
    assert request.status == "ISSUED"
    assert request.units_issued == 1
    assert unit.status == "ISSUED"
    assert issue.collected_by == "Nurse Test"

    transfusion = request_service.record_transfusion(
        db,
        officer,
        issue_id=issue.id,
        outcome="COMPLETED",
        reaction_type="NONE",
    )

    db.refresh(request)
    db.refresh(unit)
    assert transfusion.issue_id == issue.id
    assert request.status == "CLOSED"
    assert request.closed_at is not None
    assert unit.status == "TRANSFUSED"
    demand = db.scalar(
        select(DemandEvent).where(DemandEvent.blood_request_id == request.id)
    )
    assert demand.units_requested == 1
    assert demand.units_issued == 1
    assert demand.outcome == "FULFILLED"
    assert db.scalar(
        select(AuditLog).where(AuditLog.action == "TRANSFUSION_RECORDED")
    )


def test_expired_crossmatch_cannot_be_issued(db, officer):
    request = make_request(db, officer)
    unit = add_unit(db, unit_id="unit-expired-xm", group_id=1)

    request_service.record_crossmatch(
        db,
        officer,
        request_id=request.id,
        unit_id=unit.id,
        result="COMPATIBLE",
        method="GEL_CARD",
        now=DEMO_DATETIME - timedelta(days=4),
    )

    with pytest.raises(ServiceError) as raised:
        request_service.issue_unit(
            db,
            officer,
            request_id=request.id,
            unit_id=unit.id,
            collected_by="Nurse Test",
            patient_ref_confirmation=request.patient_ref,
        )

    assert raised.value.code == "CROSSMATCH_EXPIRED"


def test_accepted_return_reopens_requirement_and_stock(db, officer):
    request = make_request(db, officer)
    unit = add_unit(db, unit_id="unit-return-ok", group_id=1)
    issue = crossmatch_and_issue(db, officer, request, unit)

    returned = request_service.record_return(
        db,
        officer,
        issue_id=issue.id,
        minutes_out_of_storage=18,
        cold_chain_intact=True,
        reason="Procedure postponed",
    )

    db.refresh(request)
    db.refresh(unit)
    assert returned.return_accepted is True
    assert unit.status == "AVAILABLE"
    assert request.units_issued == 0
    assert request.status == "PENDING"
    demand = db.scalar(
        select(DemandEvent).where(DemandEvent.blood_request_id == request.id)
    )
    assert demand.units_issued == 0
    assert demand.outcome == "UNFULFILLED"


def test_failed_return_is_discarded_and_cannot_be_transfused(db, officer):
    request = make_request(db, officer)
    unit = add_unit(db, unit_id="unit-return-fail", group_id=1)
    issue = crossmatch_and_issue(db, officer, request, unit)

    request_service.record_return(
        db,
        officer,
        issue_id=issue.id,
        minutes_out_of_storage=45,
        cold_chain_intact=False,
        reason="Returned unused after procedure",
    )

    db.refresh(unit)
    assert unit.status == "DISCARDED"
    assert unit.discard_reason == "BROKEN_COLD_CHAIN"

    with pytest.raises(ServiceError) as raised:
        request_service.record_transfusion(
            db,
            officer,
            issue_id=issue.id,
            outcome="COMPLETED",
        )

    assert raised.value.code == "CUSTODY_ALREADY_CLOSED"


def test_reaction_requires_severity_and_clinical_note(db, officer):
    request = make_request(db, officer)
    unit = add_unit(db, unit_id="unit-reaction", group_id=1)
    issue = crossmatch_and_issue(db, officer, request, unit)

    with pytest.raises(ServiceError) as raised:
        request_service.record_transfusion(
            db,
            officer,
            issue_id=issue.id,
            outcome="STOPPED",
            reaction_type="ALLERGIC",
        )

    assert raised.value.code == "REACTION_SEVERITY_REQUIRED"

    record = request_service.record_transfusion(
        db,
        officer,
        issue_id=issue.id,
        outcome="STOPPED",
        reaction_type="ALLERGIC",
        reaction_severity="MODERATE",
        reaction_notes="Urticaria observed; transfusion stopped and clinician notified.",
    )
    assert record.reaction_reported_at is not None


def test_cancelling_releases_crossmatched_units(db, officer):
    request = make_request(db, officer)
    unit = add_unit(db, unit_id="unit-cancelled", group_id=1)
    request_service.record_crossmatch(
        db,
        officer,
        request_id=request.id,
        unit_id=unit.id,
        result="COMPATIBLE",
        method="GEL_CARD",
    )

    request_service.cancel_request(
        db,
        officer,
        request_id=request.id,
        reason="Clinical team cancelled the planned procedure.",
    )

    db.refresh(request)
    db.refresh(unit)
    assert request.status == "CANCELLED"
    assert unit.status == "AVAILABLE"
    demand = db.scalar(
        select(DemandEvent).where(DemandEvent.blood_request_id == request.id)
    )
    assert demand.outcome == "CANCELLED"


def test_bench_role_cannot_manage_requests(db, officer):
    phlebotomist = Actor(
        user_id="phleb-one",
        display_name="Test Phlebotomist",
        role="PHLEBOTOMIST",
        facility_id=officer.facility_id,
    )

    with pytest.raises(PermissionDenied):
        make_request(db, phlebotomist)


def test_request_cannot_cross_tenant_boundary(db, officer):
    foreign_actor = Actor(
        user_id="foreign-officer",
        display_name="Foreign Officer",
        role="BLOOD_BANK_OFFICER",
        facility_id="facility-two",
        organization_id="org-two",
    )
    request = make_request(db, officer)
    unit = add_unit(db, unit_id="unit-own-tenant", group_id=1)

    with pytest.raises(ServiceError) as raised:
        request_service.record_crossmatch(
            db,
            foreign_actor,
            request_id=request.id,
            unit_id=unit.id,
            result="COMPATIBLE",
            method="GEL_CARD",
        )

    assert raised.value.code == "REQUEST_NOT_FOUND"


def test_issue_and_transfusion_rows_are_linked_to_same_request(db, officer):
    request = make_request(db, officer)
    unit = add_unit(db, unit_id="unit-linked", group_id=1)
    issue = crossmatch_and_issue(db, officer, request, unit)
    request_service.record_transfusion(
        db,
        officer,
        issue_id=issue.id,
        outcome="COMPLETED",
    )

    saved_issue = db.get(UnitIssue, issue.id)
    saved_transfusion = db.scalar(
        select(TransfusionRecord).where(TransfusionRecord.issue_id == issue.id)
    )
    saved_request = db.get(BloodRequest, request.id)

    assert saved_issue.request_id == saved_request.id
    assert saved_transfusion.request_id == saved_request.id
    assert saved_issue.blood_unit_id == saved_transfusion.blood_unit_id

    trace = traceability.trace_unit(db, officer, unit_id=unit.id)
    stages = [step.stage for step in trace["steps"]]
    assert "ISSUED" in stages
    assert "TRANSFUSED" in stages
    issued_step = next(step for step in trace["steps"] if step.stage == "ISSUED")
    assert issued_step.detail["request_code"] == saved_request.request_code


def test_two_identifier_handover_rejects_wrong_patient_reference(db, officer):
    request = make_request(db, officer)
    unit = add_unit(db, unit_id="unit-two-id", group_id=1)
    request_service.record_crossmatch(
        db,
        officer,
        request_id=request.id,
        unit_id=unit.id,
        result="COMPATIBLE",
        method="GEL_CARD",
    )

    with pytest.raises(ServiceError) as raised:
        request_service.issue_unit(
            db,
            officer,
            request_id=request.id,
            unit_id=unit.id,
            collected_by="Nurse Test",
            patient_ref_confirmation="WRONG-EPISODE",
        )

    assert raised.value.code == "PATIENT_REF_MISMATCH"
    db.refresh(unit)
    assert unit.status == "CROSSMATCHED"


def test_expiry_reconciler_releases_stale_allocation(db, officer):
    request = make_request(db, officer)
    unit = add_unit(db, unit_id="unit-expiry-job", group_id=1)
    request_service.record_crossmatch(
        db,
        officer,
        request_id=request.id,
        unit_id=unit.id,
        result="COMPATIBLE",
        method="GEL_CARD",
        now=DEMO_DATETIME - timedelta(days=4),
    )

    expired = request_service.expire_crossmatches(
        db,
        Actor.system("test-crossmatch-expiry"),
        facility_id=officer.facility_id,
    )

    db.refresh(request)
    db.refresh(unit)
    assert expired == 1
    assert request.status == "PENDING"
    assert unit.status == "AVAILABLE"
    assert db.scalar(
        select(AuditLog).where(AuditLog.action == "CROSSMATCHES_EXPIRED")
    )


def test_allocated_request_cannot_change_group_or_shrink_below_commitment(db, officer):
    request = make_request(db, officer, units=2)
    unit = add_unit(db, unit_id="unit-edit-guard", group_id=1)
    request_service.record_crossmatch(
        db,
        officer,
        request_id=request.id,
        unit_id=unit.id,
        result="COMPATIBLE",
        method="GEL_CARD",
    )

    with pytest.raises(ServiceError) as changed_group:
        request_service.update_request(
            db,
            officer,
            request_id=request.id,
            patient_ref=request.patient_ref,
            patient_blood_group_id=2,
            component_id=1,
            units_requested=2,
            urgency="URGENT",
            clinical_context="OBSTETRIC",
        )
    assert changed_group.value.code == "ALLOCATED_REQUEST_IDENTITY"

    request_service.update_request(
        db,
        officer,
        request_id=request.id,
        patient_ref=request.patient_ref,
        patient_blood_group_id=1,
        component_id=1,
        units_requested=1,
        urgency="EMERGENCY",
        clinical_context="OBSTETRIC",
        ward="Emergency theatre",
    )
    db.refresh(request)
    assert request.urgency == "EMERGENCY"
    assert request.ward == "Emergency theatre"


def test_emergency_unknown_group_release_is_explicit_and_audited(db, officer):
    request = request_service.create_request(
        db,
        officer,
        patient_ref="EP-EMERGENCY-0001",
        component_id=1,
        units_requested=1,
        urgency="EMERGENCY",
        clinical_context="TRAUMA",
        ward="Resuscitation bay",
    )
    unit = add_unit(db, unit_id="unit-emergency-o-neg", group_id=4)

    candidates = request_service.emergency_candidate_units(
        db, officer, request_id=request.id
    )
    assert [row.id for row in candidates] == [unit.id]

    with pytest.raises(ServiceError) as unacknowledged:
        request_service.emergency_issue_unit(
            db,
            officer,
            request_id=request.id,
            unit_id=unit.id,
            collected_by="Nurse Emergency",
            patient_ref_confirmation=request.patient_ref,
            emergency_reason="Life-threatening haemorrhage before testing completed.",
            authorized_by="Dr Emergency",
            acknowledge_uncrossmatched=False,
        )
    assert unacknowledged.value.code == "EMERGENCY_ACKNOWLEDGEMENT_REQUIRED"

    issue = request_service.emergency_issue_unit(
        db,
        officer,
        request_id=request.id,
        unit_id=unit.id,
        collected_by="Nurse Emergency",
        patient_ref_confirmation=request.patient_ref,
        emergency_reason="Life-threatening haemorrhage before testing completed.",
        authorized_by="Dr Emergency",
        acknowledge_uncrossmatched=True,
    )
    assert issue.release_mode == "EMERGENCY_UNCROSSMATCHED"
    assert issue.emergency_authorized_by == "Dr Emergency"
    assert db.scalar(
        select(AuditLog).where(AuditLog.action == "EMERGENCY_UNIT_ISSUED")
    )

    trace = traceability.trace_unit(db, officer, unit_id=unit.id)
    issued = next(step for step in trace["steps"] if step.stage == "ISSUED")
    assert issued.detail["release_mode"] == "EMERGENCY_UNCROSSMATCHED"


def test_replacement_receipts_and_waiver_are_audited(db, officer):
    request = request_service.create_request(
        db,
        officer,
        patient_ref="EP-REPLACEMENT-0001",
        patient_blood_group_id=1,
        component_id=1,
        units_requested=2,
        urgency="ROUTINE",
        clinical_context="SURGERY",
        replacement_units_required=2,
    )

    request_service.record_replacement_receipt(
        db,
        officer,
        request_id=request.id,
        units_received=1,
        source_reference="SYN-DONATION-0001",
    )
    request_service.waive_replacement_requirement(
        db,
        officer,
        request_id=request.id,
        reason="Clinical welfare waiver approved for the outstanding unit.",
    )

    db.refresh(request)
    assert request.replacement_units_received == 1
    assert request.replacement_waived is True
    actions = set(
        db.scalars(
            select(AuditLog.action).where(AuditLog.entity_id == request.id)
        ).all()
    )
    assert "REPLACEMENT_RECEIPT_RECORDED" in actions
    assert "REPLACEMENT_REQUIREMENT_WAIVED" in actions


def test_not_returned_closes_custody_and_removes_unit_from_use(db, officer):
    request = make_request(db, officer)
    unit = add_unit(db, unit_id="unit-not-returned", group_id=1)
    issue = crossmatch_and_issue(db, officer, request, unit)

    closed_issue = request_service.record_not_returned(
        db,
        officer,
        issue_id=issue.id,
        reason="Ward custody investigation could not locate the issued unit.",
        incident_reference="SYN-INCIDENT-0001",
    )

    db.refresh(request)
    db.refresh(unit)
    assert closed_issue.disposition == "NOT_RETURNED"
    assert closed_issue.custody_closed_at is not None
    assert unit.status == "DISCARDED"
    assert unit.discard_reason == "NOT_RETURNED"
    assert request.status == "CLOSED"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "UNIT_NOT_RETURNED"))


def test_partial_issue_remains_open_until_clinically_closed(db, officer):
    request = make_request(db, officer, units=2)
    unit = add_unit(db, unit_id="unit-partial", group_id=1)
    issue = crossmatch_and_issue(db, officer, request, unit)

    db.refresh(request)
    assert request.status == "PARTIAL"
    assert request.units_issued == 1

    request_service.record_transfusion(
        db,
        officer,
        issue_id=issue.id,
        outcome="COMPLETED",
    )
    db.refresh(request)
    assert request.status == "PARTIAL"

    request_service.close_request(
        db,
        officer,
        request_id=request.id,
        reason="Second unit no longer clinically required after reassessment.",
    )
    db.refresh(request)
    assert request.status == "CLOSED"
