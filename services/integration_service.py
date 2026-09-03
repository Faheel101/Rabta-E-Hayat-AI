"""Tenant-safe ingestion, provenance and reconciliation.

Every adapter enters here. The service owns the complete lifecycle:

    raw archive -> column mapping -> canonical validation -> preview
    -> explicit commit -> domain row + provenance -> reconciliation

The preview is durable and the commit is idempotent. Invalid rows never enter a
clinical or forecasting table, while a mixed-quality batch can still ingest its
valid rows without making the rejected remainder disappear.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import secrets
import statistics
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth import Permission
from core.clock import as_utc
from db.models import (
    ApiClient,
    BloodGroup,
    BloodUnit,
    Component,
    DemandEvent,
    Facility,
    ImportBatch,
    ImportRow,
    IntegrationArchive,
    IntegrationFeed,
    MartFacilityKpi,
    ReconciliationIssue,
    SourceProvenance,
    StorageLocation,
    new_id,
)
from services.audit import Actor, ServiceError, audited, require, snapshot
from services.common import clear_caches

MAX_PAYLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10_000
ALLOWED_DATA_TYPES = {"INVENTORY", "DEMAND"}
ALLOWED_MODES = {"MANUAL", "SFTP_CSV", "REST", "FHIR", "HL7V2", "SIMULATED"}

INVENTORY_FIELDS = [
    "source_system_ref",
    "din",
    "component_code",
    "blood_group",
    "collected_at",
    "expires_at",
    "status",
    "screening_status",
    "volume_ml",
    "is_leucodepleted",
    "is_irradiated",
]
DEMAND_FIELDS = [
    "source_system_ref",
    "requested_at",
    "component_code",
    "blood_group",
    "units_requested",
    "units_issued",
    "urgency",
    "clinical_context",
    "outcome",
]

ALIASES = {
    "source_system_ref": {
        "source_system_ref",
        "source_ref",
        "unit_id",
        "request_id",
        "external_id",
        "record_id",
    },
    "din": {"din", "donation_id", "bag_no", "bag_number"},
    "component_code": {"component_code", "component", "product", "product_type"},
    "blood_group": {"blood_group", "group", "abo_rh", "recipient_group"},
    "collected_at": {"collected_at", "collected_date", "collection_date", "collection_time"},
    "expires_at": {"expires_at", "expiry_date", "expiration_date", "expiry"},
    "status": {"status", "unit_status"},
    "screening_status": {"screening_status", "screening", "tti_status"},
    "volume_ml": {"volume_ml", "volume", "volume_mls"},
    "is_leucodepleted": {"is_leucodepleted", "leucodepleted", "leukoreduced"},
    "is_irradiated": {"is_irradiated", "irradiated"},
    "requested_at": {"requested_at", "request_date", "ordered_at", "order_date"},
    "units_requested": {"units_requested", "requested_units", "quantity", "units"},
    "units_issued": {"units_issued", "issued_units", "fulfilled_units"},
    "urgency": {"urgency", "priority"},
    "clinical_context": {"clinical_context", "context", "indication", "service_line"},
    "outcome": {"outcome", "fulfilment", "fulfillment", "request_status"},
}

COMPONENT_ALIASES = {
    "PRC": "PRBC",
    "RBC": "PRBC",
    "PACKED RED CELLS": "PRBC",
    "PLATELET": "PLT_RD",
    "PLATELETS": "PLT_RD",
    "RDP": "PLT_RD",
    "SDP": "PLT_APH",
    "PLASMA": "FFP",
    "CRYOPRECIPITATE": "CRYO",
    "WHOLE BLOOD": "WB",
}
INVENTORY_STATUSES = {
    "QUARANTINE",
    "AVAILABLE",
    "RESERVED",
    "CROSSMATCHED",
    "ISSUED",
    "IN_TRANSIT",
    "EXPIRED",
    "DISCARDED",
    "TRANSFUSED",
}
SCREENING_STATUSES = {"PENDING", "PASSED", "FAILED"}
URGENCIES = {"ROUTINE", "URGENT", "EMERGENCY", "MASSIVE_TRANSFUSION"}
CLINICAL_CONTEXTS = {
    "TRAUMA",
    "SURGERY_ELECTIVE",
    "OBSTETRIC",
    "ONCOLOGY",
    "THALASSAEMIA",
    "DIALYSIS",
    "MEDICAL",
    "NEONATAL",
    "OTHER",
}
DEMAND_OUTCOMES = {"FULFILLED", "PARTIAL", "UNFULFILLED", "CANCELLED"}
FORBIDDEN_COLUMNS = {
    "patient_name",
    "patient_cnic",
    "donor_name",
    "donor_cnic",
    "cnic",
    "phone",
    "mobile",
    "address",
}

BATCH_FIELDS = (
    "status",
    "mode",
    "data_type",
    "field_mapping_json",
    "total_rows",
    "valid_rows",
    "quarantined_rows",
    "rejected_rows",
    "duplicate_rows",
    "ingested_rows",
    "committed_by",
    "committed_at",
    "duration_ms",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _problem(code: str, field: str, message: str, severity: str) -> dict:
    return {
        "code": code,
        "field": field,
        "message": message,
        "severity": severity,
    }


def canonical_fields(data_type: str) -> list[str]:
    value = str(data_type or "").upper()
    if value == "INVENTORY":
        return list(INVENTORY_FIELDS)
    if value == "DEMAND":
        return list(DEMAND_FIELDS)
    raise ServiceError("DATA_TYPE_INVALID", "Choose inventory or demand data.")


def _normal_header(value: str) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def auto_mapping(headers: list[str], data_type: str) -> dict[str, str]:
    normalized = {_normal_header(header): header for header in headers}
    mapping = {}
    for field in canonical_fields(data_type):
        match = next((normalized[a] for a in ALIASES[field] if a in normalized), None)
        if match:
            mapping[field] = match
    return mapping


def _parse_datetime(value, field: str, problems: list[dict]) -> datetime | None:
    if isinstance(value, datetime):
        return as_utc(value)
    text = str(value or "").strip()
    if not text:
        problems.append(_problem("REQUIRED", field, "A date and time is required.", "REJECT"))
        return None
    try:
        if len(text) == 8 and text.isdigit():
            parsed = datetime.strptime(text, "%Y%m%d")
        elif len(text) == 14 and text.isdigit():
            parsed = datetime.strptime(text, "%Y%m%d%H%M%S")
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        problems.append(
            _problem("DATETIME_INVALID", field, "Use ISO 8601 or YYYYMMDD date format.", "REJECT")
        )
        return None
    return as_utc(parsed)


def _parse_int(
    value,
    field: str,
    problems: list[dict],
    *,
    minimum: int = 0,
    maximum: int = 100_000,
) -> int | None:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        problems.append(_problem("INTEGER_INVALID", field, "Enter a whole number.", "REJECT"))
        return None
    if parsed < minimum or parsed > maximum:
        problems.append(
            _problem(
                "INTEGER_RANGE",
                field,
                f"Value must be between {minimum} and {maximum}.",
                "QUARANTINE",
            )
        )
    return parsed


def _parse_bool(value, field: str, problems: list[dict]) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n", ""}:
        return False
    problems.append(
        _problem("BOOLEAN_INVALID", field, "Use yes/no, true/false, or 1/0.", "REJECT")
    )
    return None


def _required_text(value, field: str, problems: list[dict], maximum: int = 120) -> str:
    text = str(value or "").strip()
    if not text:
        problems.append(_problem("REQUIRED", field, "This value is required.", "REJECT"))
    if len(text) > maximum:
        problems.append(
            _problem("TOO_LONG", field, f"Use no more than {maximum} characters.", "REJECT")
        )
    return text


def _normalize_group(value) -> str:
    return str(value or "").upper().replace(" ", "").replace("POSITIVE", "+").replace("NEGATIVE", "-")


def _normalize_component(value) -> str:
    code = str(value or "").strip().upper().replace("-", "_")
    return COMPONENT_ALIASES.get(code, code)


def _row_status(problems: list[dict], duplicate: bool) -> str:
    severities = {item["severity"] for item in problems}
    if "REJECT" in severities:
        return "REJECTED"
    if "QUARANTINE" in severities:
        return "QUARANTINED"
    return "DUPLICATE" if duplicate else "VALID"


def _facility_in_tenant(
    db: Session, organization_id: str, facility_id: str
) -> Facility:
    facility = db.scalar(
        select(Facility).where(
            Facility.id == facility_id,
            Facility.organization_id == organization_id,
            Facility.is_active.is_(True),
        )
    )
    if facility is None:
        raise ServiceError("FACILITY_NOT_FOUND", "Facility not found in this organization.")
    return facility


def _authorize_actor(actor: Actor, organization_id: str, facility_id: str | None = None) -> None:
    require(actor, Permission.MANAGE_INTEGRATIONS, "manage data integrations")
    if actor.organization_id and actor.organization_id != organization_id:
        raise ServiceError("TENANT_SCOPE", "Integration record not found in this organization.")
    if facility_id and not actor.organization_wide and actor.facility_id != facility_id:
        raise ServiceError("FACILITY_SCOPE", "Facility not found in your scope.")


def _ensure_feed(
    db: Session, organization_id: str, facility: Facility, mode: str
) -> IntegrationFeed:
    feed = db.scalar(select(IntegrationFeed).where(IntegrationFeed.facility_id == facility.id))
    now = _now()
    if feed is None:
        feed = IntegrationFeed(
            id=new_id(),
            organization_id=organization_id,
            facility_id=facility.id,
            mode=mode,
            status="NEVER_SYNCED",
            capabilities_json={
                "unit_level": mode != "AGGREGATE",
                "aggregate_only": False,
                "supports_push": mode in {"FHIR", "HL7V2", "REST", "SIMULATED"},
                "max_lookback_days": 548 if mode == "SIMULATED" else 90,
            },
            created_at=now,
            updated_at=now,
        )
        db.add(feed)
        db.flush()
    elif feed.mode != mode:
        feed.mode = mode
        feed.updated_at = now
    return feed


def _storage_location(
    db: Session, facility_id: str, component: Component, status: str
) -> StorageLocation | None:
    if status in {"ISSUED", "IN_TRANSIT", "EXPIRED", "DISCARDED", "TRANSFUSED"}:
        return None
    quarantine = status == "QUARANTINE"
    rows = list(
        db.scalars(
            select(StorageLocation)
            .where(
                StorageLocation.facility_id == facility_id,
                StorageLocation.is_active.is_(True),
                StorageLocation.is_quarantine.is_(quarantine),
                StorageLocation.target_temp_min_c <= component.storage_temp_min_c,
                StorageLocation.target_temp_max_c >= component.storage_temp_max_c,
            )
            .order_by(StorageLocation.is_out_of_range, StorageLocation.name)
        ).all()
    )
    if component.requires_agitation:
        rows = [row for row in rows if row.has_agitator]
    return next((row for row in rows if not row.is_out_of_range), None)


def _value(raw: dict, mapping: dict, field: str):
    source = mapping.get(field)
    return raw.get(source) if source else None


def _validate_inventory(
    db: Session,
    facility: Facility,
    raw: dict,
    mapping: dict,
    components: dict[str, Component],
    groups: dict[str, BloodGroup],
    seen_refs: set[str],
) -> tuple[dict, list[dict], list[dict], bool]:
    problems: list[dict] = []
    warnings: list[dict] = []
    ref = _required_text(_value(raw, mapping, "source_system_ref"), "source_system_ref", problems)
    din = _required_text(_value(raw, mapping, "din"), "din", problems, 30)
    component_code = _normalize_component(_value(raw, mapping, "component_code"))
    group_code = _normalize_group(_value(raw, mapping, "blood_group"))
    component = components.get(component_code)
    group = groups.get(group_code)
    if component is None:
        problems.append(_problem("COMPONENT_UNKNOWN", "component_code", "Component code is not in reference data.", "REJECT"))
    if group is None:
        problems.append(_problem("BLOOD_GROUP_UNKNOWN", "blood_group", "Blood group must be one of the eight ABO/Rh groups.", "REJECT"))

    collected = _parse_datetime(_value(raw, mapping, "collected_at"), "collected_at", problems)
    expires = _parse_datetime(_value(raw, mapping, "expires_at"), "expires_at", problems)
    status_value = str(_value(raw, mapping, "status") or "AVAILABLE").strip().upper()
    screening = str(_value(raw, mapping, "screening_status") or "PASSED").strip().upper()
    if status_value not in INVENTORY_STATUSES:
        problems.append(_problem("STATUS_INVALID", "status", "Unit status is not supported.", "REJECT"))
    if screening not in SCREENING_STATUSES:
        problems.append(_problem("SCREENING_INVALID", "screening_status", "Screening status is not supported.", "REJECT"))
    volume = _parse_int(_value(raw, mapping, "volume_ml"), "volume_ml", problems, minimum=50, maximum=800)
    leucodepleted = _parse_bool(_value(raw, mapping, "is_leucodepleted"), "is_leucodepleted", problems)
    irradiated = _parse_bool(_value(raw, mapping, "is_irradiated"), "is_irradiated", problems)

    if collected and expires:
        if expires <= collected:
            problems.append(_problem("EXPIRY_ORDER", "expires_at", "Expiry must be after collection.", "REJECT"))
        elif component:
            actual_days = (expires - collected).total_seconds() / 86400
            if actual_days > component.shelf_life_days * 1.1:
                problems.append(
                    _problem(
                        "SHELF_LIFE_IMPLAUSIBLE",
                        "expires_at",
                        f"Declared shelf life exceeds the {component.shelf_life_days}-day policy by more than 10%.",
                        "QUARANTINE",
                    )
                )
    if status_value in {"AVAILABLE", "RESERVED", "CROSSMATCHED"} and screening != "PASSED":
        problems.append(
            _problem(
                "UNSCREENED_USABLE_STOCK",
                "screening_status",
                "Usable stock must have passed screening.",
                "QUARANTINE",
            )
        )

    if ref in seen_refs:
        problems.append(_problem("DUPLICATE_IN_FILE", "source_system_ref", "This source reference appears more than once in the file.", "REJECT"))
    if ref:
        seen_refs.add(ref)

    existing = None
    if ref:
        existing = db.scalar(
            select(BloodUnit).where(
                BloodUnit.facility_id == facility.id,
                BloodUnit.source_system_ref == ref,
            )
        )
    din_owner = db.scalar(select(BloodUnit).where(BloodUnit.din == din)) if din else None
    if din_owner is not None and (existing is None or din_owner.id != existing.id):
        problems.append(_problem("DIN_COLLISION", "din", "This DIN already belongs to another physical unit.", "REJECT"))

    if existing is not None and component and group and collected and expires:
        conflicts = {}
        if existing.din != din:
            conflicts["din"] = [existing.din, din]
        if existing.component_id != component.id:
            conflicts["component_code"] = [existing.component_id, component.id]
        if existing.blood_group_id != group.id:
            conflicts["blood_group"] = [existing.blood_group_id, group.id]
        if existing.collected_at != collected:
            conflicts["collected_at"] = [existing.collected_at.isoformat(), collected.isoformat()]
        if existing.expires_at != expires:
            conflicts["expires_at"] = [existing.expires_at.isoformat(), expires.isoformat()]
        if existing.status != status_value:
            conflicts["status"] = [existing.status, status_value]
        if conflicts:
            problems.append(
                _problem(
                    "EXISTING_UNIT_CONFLICT",
                    "source_system_ref",
                    "The source record conflicts with the existing physical unit and requires reconciliation.",
                    "QUARANTINE",
                )
            )
            warnings.append({"code": "CONFLICT_FIELDS", "fields": conflicts})

    if component and status_value in {"AVAILABLE", "RESERVED", "CROSSMATCHED", "QUARANTINE"}:
        if _storage_location(db, facility.id, component, status_value) is None:
            problems.append(
                _problem(
                    "NO_COMPATIBLE_STORAGE",
                    "component_code",
                    "No active in-range storage location can safely hold this unit.",
                    "QUARANTINE",
                )
            )

    normalized = {
        "source_system_ref": ref,
        "din": din,
        "component_code": component_code,
        "blood_group": group_code,
        "collected_at": collected.isoformat() if collected else None,
        "expires_at": expires.isoformat() if expires else None,
        "status": status_value,
        "screening_status": screening,
        "volume_ml": volume,
        "is_leucodepleted": leucodepleted,
        "is_irradiated": irradiated,
    }
    return normalized, problems, warnings, existing is not None


def _validate_demand(
    db: Session,
    facility: Facility,
    raw: dict,
    mapping: dict,
    components: dict[str, Component],
    groups: dict[str, BloodGroup],
    seen_refs: set[str],
) -> tuple[dict, list[dict], list[dict], bool]:
    problems: list[dict] = []
    warnings: list[dict] = []
    ref = _required_text(_value(raw, mapping, "source_system_ref"), "source_system_ref", problems)
    requested_at = _parse_datetime(_value(raw, mapping, "requested_at"), "requested_at", problems)
    component_code = _normalize_component(_value(raw, mapping, "component_code"))
    group_code = _normalize_group(_value(raw, mapping, "blood_group"))
    if component_code not in components:
        problems.append(_problem("COMPONENT_UNKNOWN", "component_code", "Component code is not in reference data.", "REJECT"))
    if group_code not in groups:
        problems.append(_problem("BLOOD_GROUP_UNKNOWN", "blood_group", "Blood group must be one of the eight ABO/Rh groups.", "REJECT"))
    requested = _parse_int(_value(raw, mapping, "units_requested"), "units_requested", problems, minimum=1, maximum=500)
    issued = _parse_int(_value(raw, mapping, "units_issued"), "units_issued", problems, minimum=0, maximum=500)
    urgency = str(_value(raw, mapping, "urgency") or "ROUTINE").strip().upper().replace(" ", "_")
    context = str(_value(raw, mapping, "clinical_context") or "OTHER").strip().upper().replace(" ", "_")
    outcome = str(_value(raw, mapping, "outcome") or "UNFULFILLED").strip().upper()
    if urgency not in URGENCIES:
        problems.append(_problem("URGENCY_INVALID", "urgency", "Urgency is not supported.", "REJECT"))
    if context not in CLINICAL_CONTEXTS:
        problems.append(_problem("CONTEXT_INVALID", "clinical_context", "Clinical context is not supported.", "REJECT"))
    if outcome not in DEMAND_OUTCOMES:
        problems.append(_problem("OUTCOME_INVALID", "outcome", "Demand outcome is not supported.", "REJECT"))
    if requested is not None and issued is not None and issued > requested:
        problems.append(
            _problem(
                "ISSUED_EXCEEDS_REQUESTED",
                "units_issued",
                "Issued units cannot exceed requested units.",
                "QUARANTINE",
            )
        )
    if outcome == "FULFILLED" and requested is not None and issued is not None and issued < requested:
        problems.append(
            _problem(
                "OUTCOME_MISMATCH",
                "outcome",
                "A fulfilled request must issue every requested unit.",
                "QUARANTINE",
            )
        )
    if ref in seen_refs:
        problems.append(_problem("DUPLICATE_IN_FILE", "source_system_ref", "This source reference appears more than once in the file.", "REJECT"))
    if ref:
        seen_refs.add(ref)
    existing = None
    if ref:
        existing = db.scalar(
            select(DemandEvent).where(
                DemandEvent.facility_id == facility.id,
                DemandEvent.source_system_ref == ref,
            )
        )
    normalized = {
        "source_system_ref": ref,
        "requested_at": requested_at.isoformat() if requested_at else None,
        "component_code": component_code,
        "blood_group": group_code,
        "units_requested": requested,
        "units_issued": issued,
        "urgency": urgency,
        "clinical_context": context,
        "outcome": outcome,
    }
    return normalized, problems, warnings, existing is not None


def _volume_anomalies(db: Session, facility_id: str, candidates: list[dict]) -> None:
    incoming = defaultdict(int)
    for item in candidates:
        normalized = item["normalized"]
        if normalized.get("requested_at") and normalized.get("units_requested") is not None:
            incoming[datetime.fromisoformat(normalized["requested_at"]).date()] += int(
                normalized["units_requested"]
            )
    if not incoming:
        return
    latest = db.scalar(
        select(func.max(DemandEvent.requested_at)).where(DemandEvent.facility_id == facility_id)
    )
    if latest is None:
        return
    since = latest - timedelta(days=90)
    history_rows = db.execute(
        select(
            func.date(DemandEvent.requested_at),
            func.sum(DemandEvent.units_requested),
        )
        .where(DemandEvent.facility_id == facility_id, DemandEvent.requested_at >= since)
        .group_by(func.date(DemandEvent.requested_at))
    ).all()
    values = [int(row[1] or 0) for row in history_rows]
    if len(values) < 14:
        return
    mean = statistics.fmean(values)
    deviation = statistics.pstdev(values)
    threshold = mean + 5 * max(deviation, 1.0)
    anomalous_dates = {day for day, total in incoming.items() if total > threshold}
    if not anomalous_dates:
        return
    for item in candidates:
        value = item["normalized"].get("requested_at")
        if value and datetime.fromisoformat(value).date() in anomalous_dates:
            item["problems"].append(
                _problem(
                    "DAILY_VOLUME_OUTLIER",
                    "units_requested",
                    f"Batch volume exceeds the facility's 90-day mean plus five standard deviations ({threshold:.1f} units).",
                    "QUARANTINE",
                )
            )


def _validate_rows(
    db: Session,
    facility: Facility,
    data_type: str,
    rows: list[dict],
    mapping: dict,
) -> list[dict]:
    components = {row.code.upper(): row for row in db.scalars(select(Component)).all()}
    groups = {row.code.upper(): row for row in db.scalars(select(BloodGroup)).all()}
    seen_refs: set[str] = set()
    validated = []
    validator = _validate_inventory if data_type == "INVENTORY" else _validate_demand
    for number, raw in enumerate(rows, start=2):
        normalized, problems, warnings, duplicate = validator(
            db, facility, raw, mapping, components, groups, seen_refs
        )
        validated.append(
            {
                "row_number": number,
                "raw": raw,
                "normalized": normalized,
                "problems": problems,
                "warnings": warnings,
                "duplicate": duplicate,
            }
        )
    if data_type == "DEMAND":
        _volume_anomalies(db, facility.id, validated)
    return validated


def _summarize(candidates: list[dict]) -> dict:
    counts = defaultdict(int)
    codes = defaultdict(int)
    for item in candidates:
        status = _row_status(item["problems"], item["duplicate"])
        counts[status] += 1
        for problem in item["problems"]:
            codes[problem["code"]] += 1
    return {
        "total": len(candidates),
        "valid": counts["VALID"],
        "quarantined": counts["QUARANTINED"],
        "rejected": counts["REJECTED"],
        "duplicates": counts["DUPLICATE"],
        "problem_codes": dict(sorted(codes.items(), key=lambda item: (-item[1], item[0]))),
    }


def _persist_candidates(db: Session, batch: ImportBatch, candidates: list[dict]) -> dict:
    summary = _summarize(candidates)
    for item in candidates:
        status = _row_status(item["problems"], item["duplicate"])
        normalized = item["normalized"]
        import_row = ImportRow(
                id=new_id(),
                batch_id=batch.id,
                row_number=item["row_number"],
                source_system_ref=normalized.get("source_system_ref"),
                raw_json=item["raw"],
                normalized_json=normalized,
                payload_hash=_sha(_canonical_json(normalized)),
                status=status,
                errors_json=item["problems"],
                warnings_json=item["warnings"],
            )
        db.add(import_row)
        quarantine_problems = [
            problem
            for problem in item["problems"]
            if problem.get("severity") == "QUARANTINE"
        ]
        if quarantine_problems:
            codes = [problem["code"] for problem in quarantine_problems]
            db.add(
                ReconciliationIssue(
                    id=new_id(),
                    organization_id=batch.organization_id,
                    facility_id=batch.facility_id,
                    batch_id=batch.id,
                    import_row_id=import_row.id,
                    issue_type=codes[0],
                    severity=(
                        "CRITICAL"
                        if "UNSCREENED_USABLE_STOCK" in codes
                        else "WARNING"
                    ),
                    status="OPEN",
                    summary=quarantine_problems[0]["message"][:255],
                    details_json={
                        "row_number": item["row_number"],
                        "source_system_ref": normalized.get("source_system_ref"),
                        "problems": quarantine_problems,
                    },
                )
            )
    batch.total_rows = summary["total"]
    batch.valid_rows = summary["valid"]
    batch.quarantined_rows = summary["quarantined"]
    batch.rejected_rows = summary["rejected"]
    batch.duplicate_rows = summary["duplicates"]
    batch.validation_summary_json = summary
    batch.status = (
        "READY"
        if summary["quarantined"] == 0 and summary["rejected"] == 0
        else "NEEDS_REVIEW"
    )
    return summary


def _decode_csv(payload: bytes) -> tuple[str, list[str], list[dict]]:
    if not payload:
        raise ServiceError("EMPTY_FILE", "The uploaded file is empty.")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ServiceError("FILE_TOO_LARGE", "Upload a CSV smaller than 5 MB.")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ServiceError("ENCODING_INVALID", "Save the CSV with UTF-8 encoding.") from exc
    reader = csv.DictReader(io.StringIO(text))
    headers = [str(value or "").strip() for value in (reader.fieldnames or [])]
    if not headers:
        raise ServiceError("HEADERS_MISSING", "The CSV must include a header row.")
    forbidden = sorted({_normal_header(value) for value in headers} & FORBIDDEN_COLUMNS)
    if forbidden:
        raise ServiceError(
            "IDENTITY_FIELDS_FORBIDDEN",
            "Remove patient/donor identity columns before upload: " + ", ".join(forbidden),
        )
    rows = []
    for row in reader:
        if all(str(value or "").strip() == "" for value in row.values()):
            continue
        rows.append({str(key): value for key, value in row.items()})
        if len(rows) > MAX_ROWS:
            raise ServiceError("ROW_LIMIT", "A preview may contain at most 10,000 rows.")
    if not rows:
        raise ServiceError("NO_DATA_ROWS", "The CSV contains no data rows.")
    return text, headers, rows


def preview_csv(
    db: Session,
    actor: Actor,
    *,
    organization_id: str,
    facility_id: str,
    data_type: str,
    filename: str,
    payload: bytes,
) -> ImportBatch:
    text, headers, rows = _decode_csv(payload)
    return preview_records(
        db,
        actor,
        organization_id=organization_id,
        facility_id=facility_id,
        data_type=data_type,
        mode="MANUAL",
        filename=filename,
        content_type="text/csv",
        raw_payload=text,
        rows=rows,
        headers=headers,
    )


def preview_records(
    db: Session,
    actor: Actor,
    *,
    organization_id: str,
    facility_id: str,
    data_type: str,
    mode: str,
    filename: str,
    content_type: str,
    raw_payload: str,
    rows: list[dict],
    headers: list[str] | None = None,
) -> ImportBatch:
    data_type = str(data_type).upper()
    mode = str(mode).upper()
    if data_type not in ALLOWED_DATA_TYPES:
        raise ServiceError("DATA_TYPE_INVALID", "Choose inventory or demand data.")
    if mode not in ALLOWED_MODES:
        raise ServiceError("ADAPTER_MODE_INVALID", "Adapter mode is not supported.")
    encoded = raw_payload.encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ServiceError("FILE_TOO_LARGE", "The source payload must be smaller than 5 MB.")
    if not rows:
        raise ServiceError("NO_DATA_ROWS", "The source contains no supported records.")
    if len(rows) > MAX_ROWS:
        raise ServiceError("ROW_LIMIT", "A preview may contain at most 10,000 rows.")
    _authorize_actor(actor, organization_id, facility_id)
    facility = _facility_in_tenant(db, organization_id, facility_id)
    checksum = _sha(encoded)
    existing = db.scalar(
        select(ImportBatch).where(
            ImportBatch.facility_id == facility_id,
            ImportBatch.mode == mode,
            ImportBatch.data_type == data_type,
            ImportBatch.checksum_sha256 == checksum,
        )
    )
    if existing is not None:
        return existing

    source_headers = headers or list(dict.fromkeys(key for row in rows for key in row.keys()))
    mapping = auto_mapping(source_headers, data_type)
    now = _now()
    with audited(db, actor, "integration.preview", "import_batch") as entry:
        feed = _ensure_feed(db, organization_id, facility, mode)
        batch = ImportBatch(
            id=new_id(),
            organization_id=organization_id,
            facility_id=facility_id,
            feed_id=feed.id,
            mode=mode,
            data_type=data_type,
            status="PREVIEW",
            filename=(filename or "source-payload")[:255],
            content_type=content_type[:120],
            checksum_sha256=checksum,
            payload_bytes=len(encoded),
            source_headers_json=source_headers,
            field_mapping_json=mapping,
            created_by=actor.display_name,
            created_at=now,
        )
        db.add(batch)
        db.flush()
        db.add(
            IntegrationArchive(
                id=new_id(),
                batch_id=batch.id,
                checksum_sha256=checksum,
                content_type=content_type[:120],
                payload_text=raw_payload,
                payload_bytes=len(encoded),
                created_at=now,
            )
        )
        candidates = _validate_rows(db, facility, data_type, rows, mapping)
        summary = _persist_candidates(db, batch, candidates)
        feed.rows_seen += len(rows)
        feed.updated_at = now
        entry.on(batch, after=snapshot(batch, BATCH_FIELDS))
        entry.note(filename=batch.filename, checksum=checksum, summary=summary)
    clear_caches()
    return batch


def get_batch(db: Session, organization_id: str, batch_id: str) -> ImportBatch:
    batch = db.scalar(
        select(ImportBatch).where(
            ImportBatch.id == batch_id,
            ImportBatch.organization_id == organization_id,
        )
    )
    if batch is None:
        raise ServiceError("BATCH_NOT_FOUND", "Import batch not found.")
    return batch


def remap_batch(
    db: Session,
    actor: Actor,
    *,
    organization_id: str,
    batch_id: str,
    mapping: dict[str, str],
) -> ImportBatch:
    batch = get_batch(db, organization_id, batch_id)
    _authorize_actor(actor, organization_id, batch.facility_id)
    if batch.status not in {"READY", "NEEDS_REVIEW", "PREVIEW"}:
        raise ServiceError("BATCH_LOCKED", "A committed batch cannot be remapped.")
    allowed_headers = set(batch.source_headers_json or [])
    cleaned = {
        field: source
        for field, source in mapping.items()
        if field in canonical_fields(batch.data_type) and source in allowed_headers
    }
    facility = _facility_in_tenant(db, organization_id, batch.facility_id)
    source_rows = list(
        db.scalars(
            select(ImportRow).where(ImportRow.batch_id == batch.id).order_by(ImportRow.row_number)
        ).all()
    )
    raw_rows = [row.raw_json for row in source_rows]
    before = snapshot(batch, BATCH_FIELDS)
    with audited(db, actor, "integration.remap", "import_batch", batch.id) as entry:
        db.execute(
            delete(ReconciliationIssue).where(
                ReconciliationIssue.batch_id == batch.id,
                ReconciliationIssue.status == "OPEN",
            )
        )
        db.execute(delete(ImportRow).where(ImportRow.batch_id == batch.id))
        db.flush()
        batch.field_mapping_json = cleaned
        candidates = _validate_rows(db, facility, batch.data_type, raw_rows, cleaned)
        summary = _persist_candidates(db, batch, candidates)
        entry.on(batch, before=before, after=snapshot(batch, BATCH_FIELDS))
        entry.note(summary=summary)
    clear_caches()
    return batch


def _provenance(
    db: Session,
    *,
    batch: ImportBatch,
    row: ImportRow,
    entity_type: str,
    entity_id: str,
    now: datetime,
) -> SourceProvenance:
    record = db.scalar(
        select(SourceProvenance).where(
            SourceProvenance.facility_id == batch.facility_id,
            SourceProvenance.source_mode == batch.mode,
            SourceProvenance.source_system_ref == row.source_system_ref,
            SourceProvenance.entity_type == entity_type,
        )
    )
    if record is None:
        record = SourceProvenance(
            id=new_id(),
            organization_id=batch.organization_id,
            facility_id=batch.facility_id,
            batch_id=batch.id,
            source_mode=batch.mode,
            source_system_ref=str(row.source_system_ref),
            entity_type=entity_type,
            entity_id=entity_id,
            payload_hash=str(row.payload_hash),
            first_seen_at=now,
            last_seen_at=now,
            version_count=1,
        )
        db.add(record)
    else:
        if record.payload_hash != row.payload_hash:
            record.version_count += 1
        record.batch_id = batch.id
        record.entity_id = entity_id
        record.payload_hash = str(row.payload_hash)
        record.last_seen_at = now
    return record


def _commit_inventory_row(
    db: Session,
    batch: ImportBatch,
    row: ImportRow,
    components: dict[str, Component],
    groups: dict[str, BloodGroup],
    now: datetime,
) -> tuple[BloodUnit, bool]:
    value = row.normalized_json
    component = components[value["component_code"]]
    group = groups[value["blood_group"]]
    existing = db.scalar(
        select(BloodUnit).where(
            BloodUnit.facility_id == batch.facility_id,
            BloodUnit.source_system_ref == value["source_system_ref"],
        )
    )
    created = existing is None
    if existing is None:
        location = _storage_location(db, batch.facility_id, component, value["status"])
        existing = BloodUnit(
            id=new_id(),
            din=value["din"],
            facility_id=batch.facility_id,
            component_id=component.id,
            blood_group_id=group.id,
            volume_ml=value["volume_ml"],
            collected_at=datetime.fromisoformat(value["collected_at"]),
            expires_at=datetime.fromisoformat(value["expires_at"]),
            status=value["status"],
            screening_status=value["screening_status"],
            is_leucodepleted=value["is_leucodepleted"],
            is_irradiated=value["is_irradiated"],
            source_system_ref=value["source_system_ref"],
            last_synced_at=now,
            storage_location_id=location.id if location else None,
        )
        db.add(existing)
        db.flush()
    else:
        # Identity, clinical classification and operational state were compared
        # during preview. Only source-owned descriptive fields are refreshed;
        # a feed cannot bypass reservation, issue or transfer state machines.
        existing.volume_ml = value["volume_ml"]
        existing.is_leucodepleted = value["is_leucodepleted"]
        existing.is_irradiated = value["is_irradiated"]
        existing.screening_status = value["screening_status"]
        existing.last_synced_at = now
    return existing, created


def _commit_demand_row(
    db: Session,
    batch: ImportBatch,
    row: ImportRow,
    components: dict[str, Component],
    groups: dict[str, BloodGroup],
    now: datetime,
) -> tuple[DemandEvent, bool]:
    value = row.normalized_json
    component = components[value["component_code"]]
    group = groups[value["blood_group"]]
    existing = db.scalar(
        select(DemandEvent).where(
            DemandEvent.facility_id == batch.facility_id,
            DemandEvent.source_system_ref == value["source_system_ref"],
        )
    )
    created = existing is None
    if existing is None:
        existing = DemandEvent(
            id=new_id(),
            facility_id=batch.facility_id,
            component_id=component.id,
            blood_group_id=group.id,
            requested_at=datetime.fromisoformat(value["requested_at"]),
            units_requested=value["units_requested"],
            units_issued=value["units_issued"],
            urgency=value["urgency"],
            clinical_context=value["clinical_context"],
            was_substituted=False,
            outcome=value["outcome"],
            source_system_ref=value["source_system_ref"],
            last_synced_at=now,
        )
        db.add(existing)
        db.flush()
    else:
        existing.component_id = component.id
        existing.blood_group_id = group.id
        existing.requested_at = datetime.fromisoformat(value["requested_at"])
        existing.units_requested = value["units_requested"]
        existing.units_issued = value["units_issued"]
        existing.urgency = value["urgency"]
        existing.clinical_context = value["clinical_context"]
        existing.outcome = value["outcome"]
        existing.last_synced_at = now
    return existing, created


def commit_batch(
    db: Session,
    actor: Actor,
    *,
    organization_id: str,
    batch_id: str,
) -> ImportBatch:
    batch = get_batch(db, organization_id, batch_id)
    _authorize_actor(actor, organization_id, batch.facility_id)
    if batch.status in {"COMMITTED", "COMMITTED_WITH_QUARANTINE"}:
        return batch
    if batch.status not in {"READY", "NEEDS_REVIEW"}:
        raise ServiceError("BATCH_NOT_READY", "Validate the batch before committing it.")
    rows = list(
        db.scalars(
            select(ImportRow)
            .where(ImportRow.batch_id == batch.id)
            .order_by(ImportRow.row_number)
        ).all()
    )
    eligible = [row for row in rows if row.status in {"VALID", "DUPLICATE"}]
    if not eligible:
        raise ServiceError(
            "NO_VALID_ROWS",
            "This batch has no valid rows. Correct the quarantined/rejected records and preview it again.",
        )
    components = {row.code: row for row in db.scalars(select(Component)).all()}
    groups = {row.code: row for row in db.scalars(select(BloodGroup)).all()}
    now = _now()
    started = time.perf_counter()
    before = snapshot(batch, BATCH_FIELDS)
    created_count = 0
    updated_count = 0
    with audited(db, actor, "integration.commit", "import_batch", batch.id) as entry:
        for row in eligible:
            if batch.data_type == "INVENTORY":
                entity, created = _commit_inventory_row(db, batch, row, components, groups, now)
                entity_type = "blood_unit"
            else:
                entity, created = _commit_demand_row(db, batch, row, components, groups, now)
                entity_type = "demand_event"
            created_count += int(created)
            updated_count += int(not created)
            row.entity_type = entity_type
            row.entity_id = entity.id
            row.ingested_at = now
            row.status = "INGESTED" if created else "DUPLICATE"
            _provenance(
                db,
                batch=batch,
                row=row,
                entity_type=entity_type,
                entity_id=entity.id,
                now=now,
            )
        batch.ingested_rows = len(eligible)
        batch.status = (
            "COMMITTED_WITH_QUARANTINE"
            if batch.quarantined_rows or batch.rejected_rows
            else "COMMITTED"
        )
        batch.committed_by = actor.display_name
        batch.committed_at = now
        batch.duration_ms = max(1, int((time.perf_counter() - started) * 1000))
        feed = db.get(IntegrationFeed, batch.feed_id) if batch.feed_id else None
        if feed:
            feed.last_sync_at = now
            feed.last_success_at = now
            feed.rows_ingested += len(eligible)
            feed.rows_quarantined += batch.quarantined_rows + batch.rejected_rows
            feed.status = (
                "DEGRADED"
                if batch.quarantined_rows or batch.rejected_rows
                else "HEALTHY"
            )
            feed.last_error = (
                f"{batch.quarantined_rows + batch.rejected_rows} rows require review"
                if feed.status == "DEGRADED"
                else None
            )
            feed.consecutive_failures = 0
            feed.updated_at = now
        entry.on(batch, before=before, after=snapshot(batch, BATCH_FIELDS))
        entry.note(created=created_count, updated=updated_count, skipped=len(rows) - len(eligible))
    clear_caches()
    return batch


def resolve_issue(
    db: Session,
    actor: Actor,
    *,
    organization_id: str,
    issue_id: str,
    note: str,
) -> ReconciliationIssue:
    issue = db.scalar(
        select(ReconciliationIssue).where(
            ReconciliationIssue.id == issue_id,
            ReconciliationIssue.organization_id == organization_id,
        )
    )
    if issue is None:
        raise ServiceError("ISSUE_NOT_FOUND", "Reconciliation issue not found.")
    _authorize_actor(actor, organization_id, issue.facility_id)
    if issue.status != "OPEN":
        raise ServiceError("ISSUE_CLOSED", "This reconciliation issue is already closed.")
    note = str(note or "").strip()
    if len(note) < 3:
        raise ServiceError("RESOLUTION_REQUIRED", "Record how this issue was resolved.")
    now = _now()
    with audited(db, actor, "integration.reconcile", "reconciliation_issue", issue.id) as entry:
        before = snapshot(issue, ("status", "resolved_by", "resolved_at", "resolution_note"))
        issue.status = "RESOLVED"
        issue.resolved_by = actor.display_name
        issue.resolved_at = now
        issue.resolution_note = note
        entry.on(
            issue,
            before=before,
            after=snapshot(issue, ("status", "resolved_by", "resolved_at", "resolution_note")),
        )
    return issue


def create_api_client(
    db: Session,
    actor: Actor,
    *,
    organization_id: str,
    name: str,
    scopes: list[str],
    facility_ids: list[str] | None = None,
) -> tuple[ApiClient, str]:
    _authorize_actor(actor, organization_id)
    name = str(name or "").strip()
    if len(name) < 3:
        raise ServiceError("CLIENT_NAME_REQUIRED", "Enter a descriptive client name.")
    allowed_scopes = {"facilities:read", "inventory:read", "demand:read", "imports:write"}
    cleaned_scopes = sorted(set(scopes) & allowed_scopes)
    if not cleaned_scopes:
        raise ServiceError("CLIENT_SCOPE_REQUIRED", "Select at least one API scope.")
    owned = set(
        db.scalars(
            select(Facility.id).where(Facility.organization_id == organization_id)
        ).all()
    )
    requested = set(facility_ids or [])
    if requested - owned:
        raise ServiceError("FACILITY_SCOPE", "One or more API facilities are outside this organization.")
    secret = "rh_live_" + secrets.token_urlsafe(32)
    client = ApiClient(
        id=new_id(),
        organization_id=organization_id,
        name=name[:120],
        key_prefix=secret[:16],
        key_hash=_sha(secret),
        scopes_json=cleaned_scopes,
        facility_ids_json=sorted(requested),
        created_by=actor.display_name,
        created_at=_now(),
    )
    with audited(db, actor, "integration.api_client.create", "api_client", client.id) as entry:
        db.add(client)
        entry.on(
            client,
            after={
                "name": client.name,
                "key_prefix": client.key_prefix,
                "scopes": cleaned_scopes,
                "facility_ids": client.facility_ids_json,
            },
        )
    return client, secret


def revoke_api_client(
    db: Session,
    actor: Actor,
    *,
    organization_id: str,
    client_id: str,
) -> ApiClient:
    client = db.scalar(
        select(ApiClient).where(
            ApiClient.id == client_id,
            ApiClient.organization_id == organization_id,
        )
    )
    if client is None:
        raise ServiceError("CLIENT_NOT_FOUND", "API client not found.")
    _authorize_actor(actor, organization_id)
    if not client.is_active:
        return client
    with audited(db, actor, "integration.api_client.revoke", "api_client", client.id) as entry:
        before = {"is_active": True, "revoked_at": None}
        client.is_active = False
        client.revoked_at = _now()
        entry.on(
            client,
            before=before,
            after={
                "is_active": False,
                "revoked_at": client.revoked_at.isoformat(),
            },
        )
    return client


def authenticate_api_key(db: Session, secret: str) -> ApiClient | None:
    value = str(secret or "").strip()
    if not value.startswith("rh_live_") or len(value) < 30:
        return None
    digest = _sha(value)
    client = db.scalar(
        select(ApiClient).where(
            ApiClient.key_hash == digest,
            ApiClient.is_active.is_(True),
            ApiClient.revoked_at.is_(None),
        )
    )
    if client:
        client.last_used_at = _now()
        db.commit()
    return client


def batch_detail(
    db: Session,
    organization_id: str,
    batch_id: str,
    *,
    page: int = 1,
    page_size: int = 100,
) -> dict:
    batch = get_batch(db, organization_id, batch_id)
    facility = db.get(Facility, batch.facility_id)
    total = int(
        db.scalar(select(func.count()).select_from(ImportRow).where(ImportRow.batch_id == batch.id))
        or 0
    )
    page_size = max(25, min(200, int(page_size)))
    pages = max(1, math.ceil(total / page_size))
    page = max(1, min(page, pages))
    rows = list(
        db.scalars(
            select(ImportRow)
            .where(ImportRow.batch_id == batch.id)
            .order_by(ImportRow.row_number)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    return {
        "batch": batch,
        "facility": facility,
        "rows": rows,
        "canonical_fields": canonical_fields(batch.data_type),
        "page": page,
        "pages": pages,
        "total": total,
    }


def workspace(db: Session, organization_id: str) -> dict:
    from services.feed_health_service import rows as canonical_feed_rows

    facilities = list(
        db.scalars(
            select(Facility)
            .where(Facility.organization_id == organization_id, Facility.is_active.is_(True))
            .order_by(Facility.name_en)
        ).all()
    )
    feed_rows = canonical_feed_rows(db, facilities)
    batches = list(
        db.execute(
            select(ImportBatch, Facility.name_en.label("facility_name"))
            .join(Facility, Facility.id == ImportBatch.facility_id)
            .where(ImportBatch.organization_id == organization_id)
            .order_by(ImportBatch.created_at.desc())
            .limit(30)
        ).all()
    )
    issues = list(
        db.execute(
            select(ReconciliationIssue, Facility.name_en.label("facility_name"))
            .join(Facility, Facility.id == ReconciliationIssue.facility_id)
            .where(
                ReconciliationIssue.organization_id == organization_id,
                ReconciliationIssue.status == "OPEN",
            )
            .order_by(ReconciliationIssue.created_at.desc())
            .limit(50)
        ).all()
    )
    clients = list(
        db.scalars(
            select(ApiClient)
            .where(ApiClient.organization_id == organization_id)
            .order_by(ApiClient.created_at.desc())
        ).all()
    )
    counts = {
        "facilities": len(facilities),
        "healthy": sum(1 for row in feed_rows if row["status"] == "HEALTHY"),
        "degraded": sum(1 for row in feed_rows if row["status"] in {"DEGRADED", "STALE", "OFFLINE"}),
        "quarantine": int(
            db.scalar(
                select(func.count()).select_from(ImportRow).join(
                    ImportBatch, ImportBatch.id == ImportRow.batch_id
                ).where(
                    ImportBatch.organization_id == organization_id,
                    ImportRow.status.in_({"QUARANTINED", "REJECTED"}),
                )
            )
            or 0
        ),
        "open_issues": len(issues),
    }
    return {
        "facilities": facilities,
        "feed_rows": feed_rows,
        "batches": batches,
        "issues": issues,
        "api_clients": clients,
        "counts": counts,
    }


def error_report_csv(db: Session, organization_id: str, batch_id: str) -> str:
    batch = get_batch(db, organization_id, batch_id)
    rows = list(
        db.scalars(
            select(ImportRow)
            .where(
                ImportRow.batch_id == batch.id,
                ImportRow.status.in_({"QUARANTINED", "REJECTED"}),
            )
            .order_by(ImportRow.row_number)
        ).all()
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["row_number", "source_system_ref", "status", "error_codes", "errors", "raw_json"])
    for row in rows:
        writer.writerow(
            [
                row.row_number,
                row.source_system_ref or "",
                row.status,
                ";".join(item.get("code", "") for item in row.errors_json or []),
                "; ".join(item.get("message", "") for item in row.errors_json or []),
                _canonical_json(row.raw_json or {}),
            ]
        )
    return output.getvalue()


def template_csv(data_type: str) -> str:
    fields = canonical_fields(data_type)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(fields)
    if str(data_type).upper() == "INVENTORY":
        writer.writerow(
            [
                "BBMIS-UNIT-0001",
                "G000026000001",
                "PRBC",
                "O+",
                "2026-08-15T08:00:00+00:00",
                "2026-09-15T08:00:00+00:00",
                "AVAILABLE",
                "PASSED",
                300,
                "yes",
                "no",
            ]
        )
    else:
        writer.writerow(
            [
                "HIS-REQ-0001",
                "2026-08-16T10:30:00+00:00",
                "PRBC",
                "O+",
                2,
                2,
                "URGENT",
                "TRAUMA",
                "FULFILLED",
            ]
        )
    return output.getvalue()


def bootstrap_simulated_feeds(db: Session, actor: Actor | None = None) -> int:
    """Register the seeded demo network through the production adapter model."""

    actor = actor or Actor.system("integration-bootstrap")
    facilities = list(
        db.scalars(
            select(Facility).where(
                Facility.organization_id.is_not(None),
                Facility.is_active.is_(True),
            )
        ).all()
    )
    marts = {
        row.facility_id: row
        for row in db.scalars(
            select(MartFacilityKpi).where(
                MartFacilityKpi.facility_id.in_([item.id for item in facilities] or ["__none__"])
            )
        ).all()
    }
    created = 0
    with audited(db, actor, "integration.bootstrap", "integration_feed") as entry:
        for facility in facilities:
            existing = db.scalar(
                select(IntegrationFeed).where(IntegrationFeed.facility_id == facility.id)
            )
            feed = _ensure_feed(
                db,
                str(facility.organization_id),
                facility,
                facility.integration_mode or "SIMULATED",
            )
            created += int(existing is None)
            mart = marts.get(facility.id)
            if mart:
                feed.status = mart.feed_status or "HEALTHY"
                feed.last_sync_at = mart.last_synced_at
                feed.last_success_at = mart.last_synced_at
                feed.updated_at = _now()
        entry.note(facilities=len(facilities), created=created)
    return created
