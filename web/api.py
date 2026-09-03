"""Versioned machine API with a clean, dedicated OpenAPI surface."""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse

from config.settings import APP_VERSION, DATA_NOTICE, DEMO_DATE, SYNTHETIC_DATA
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from adapters.fhir import FhirAdapterError, parse_bundle
from adapters.hl7v2 import Hl7AdapterError, parse_message
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
)
from services import integration_service
from services.audit import Actor, ServiceError
from web.deps import get_db


api_app = FastAPI(
    title="Rabta-e-Hayat Integration API",
    version="1.0.0",
    description=(
        "Tenant-scoped blood inventory, demand, import preview and adapter API. "
        "All writes use the same validation, quarantine, provenance and audit "
        "contract as the manual data workspace."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    contact={"name": "Rabta-e-Hayat Integration Operations"},
    license_info={"name": "Hackathon demonstration — synthetic data only"},
)


class ApiError(BaseModel):
    code: str
    message: str
    field: str | None = None


class FhirPreviewRequest(BaseModel):
    facility_id: str
    bundle: dict[str, Any]


class Hl7PreviewRequest(BaseModel):
    facility_id: str
    message: str = Field(min_length=12, max_length=5_000_000)


class CanonicalPreviewRequest(BaseModel):
    facility_id: str
    data_type: str
    records: list[dict[str, Any]] = Field(min_length=1, max_length=10_000)
    source_name: str = "api-payload.json"


class CommitRequest(BaseModel):
    confirm: bool = True


_RATE_WINDOWS: dict[str, deque[float]] = defaultdict(deque)


def _api_error(code: str, message: str, http_status: int, field: str | None = None):
    raise HTTPException(
        status_code=http_status,
        detail={"code": code, "message": message, "field": field},
    )


def _client(
    x_rabta_key: str | None = Header(default=None, alias="X-Rabta-Key"),
    db: Session = Depends(get_db),
) -> ApiClient:
    if not x_rabta_key:
        _api_error("API_KEY_REQUIRED", "Send an API key in X-Rabta-Key.", 401)
    client = integration_service.authenticate_api_key(db, x_rabta_key or "")
    if client is None:
        _api_error("API_KEY_INVALID", "API key is invalid or revoked.", 401)
    now = time.monotonic()
    window = _RATE_WINDOWS[client.id]
    while window and now - window[0] >= 60:
        window.popleft()
    if len(window) >= max(1, int(client.rate_limit_per_minute or 120)):
        _api_error("RATE_LIMITED", "API rate limit exceeded. Retry after one minute.", 429)
    window.append(now)
    return client


def _scope(client: ApiClient, required: str) -> None:
    if required not in set(client.scopes_json or []):
        _api_error("SCOPE_REQUIRED", f"API client requires {required} scope.", 403)


def _allowed_facilities(db: Session, client: ApiClient) -> list[str]:
    owned = set(
        db.scalars(
            select(Facility.id).where(
                Facility.organization_id == client.organization_id,
                Facility.is_active.is_(True),
            )
        ).all()
    )
    configured = set(client.facility_ids_json or [])
    return sorted(owned if not configured else owned & configured)


def _facility(db: Session, client: ApiClient, facility_id: str) -> Facility:
    if facility_id not in _allowed_facilities(db, client):
        _api_error("FACILITY_NOT_FOUND", "Facility not found in this API scope.", 404)
    facility = db.get(Facility, facility_id)
    if facility is None:
        _api_error("FACILITY_NOT_FOUND", "Facility not found in this API scope.", 404)
    return facility


def _actor(client: ApiClient, facility_id: str | None = None) -> Actor:
    return Actor(
        user_id=f"api:{client.id}",
        display_name=f"API client: {client.name}",
        role="SYSTEM_ADMIN",
        facility_id=facility_id,
        organization_id=client.organization_id,
        organization_wide=True,
    )


def _batch_payload(batch: ImportBatch) -> dict:
    return {
        "id": batch.id,
        "facility_id": batch.facility_id,
        "mode": batch.mode,
        "data_type": batch.data_type,
        "status": batch.status,
        "checksum_sha256": batch.checksum_sha256,
        "counts": {
            "total": batch.total_rows,
            "valid": batch.valid_rows,
            "duplicates": batch.duplicate_rows,
            "quarantined": batch.quarantined_rows,
            "rejected": batch.rejected_rows,
            "ingested": batch.ingested_rows,
        },
        "created_at": batch.created_at,
        "committed_at": batch.committed_at,
        "links": {
            "self": f"/api/v1/imports/{batch.id}",
            "commit": f"/api/v1/imports/{batch.id}/commit",
        },
    }


@api_app.exception_handler(ServiceError)
async def service_error_handler(_request, exc: ServiceError):
    http_status = 404 if exc.code.endswith("NOT_FOUND") else 422
    if exc.code in {"TENANT_SCOPE", "FACILITY_SCOPE", "PERMISSION_DENIED"}:
        http_status = 403
    return JSONResponse(
        status_code=http_status,
        content={"error": {"code": exc.code, "message": exc.message, "field": exc.field}},
    )


@api_app.get("/v1/health", tags=["Service"])
def health():
    return {
        "status": "ok",
        "service": "rabta-integration-api",
        "version": APP_VERSION,
        "time": datetime.now(timezone.utc),
        "data_notice": DATA_NOTICE,
        "data_mode": "synthetic" if SYNTHETIC_DATA else "live",
        "scenario_date": str(DEMO_DATE) if SYNTHETIC_DATA else None,
    }


@api_app.get("/v1/facilities", tags=["Reference"])
def facilities(
    db: Session = Depends(get_db),
    client: ApiClient = Depends(_client),
):
    _scope(client, "facilities:read")
    rows = list(
        db.scalars(
            select(Facility)
            .where(Facility.id.in_(_allowed_facilities(db, client) or ["__none__"]))
            .order_by(Facility.name_en)
        ).all()
    )
    return {
        "data": [
            {
                "id": row.id,
                "code": row.code,
                "name": row.name_en,
                "type": row.facility_type,
                "district": row.district,
                "province": row.province,
                "integration_mode": row.integration_mode,
            }
            for row in rows
        ],
        "count": len(rows),
    }


@api_app.get("/v1/inventory", tags=["Inventory"])
def inventory(
    facility_id: str,
    updated_after: datetime | None = None,
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    client: ApiClient = Depends(_client),
):
    _scope(client, "inventory:read")
    _facility(db, client, facility_id)
    statement = (
        select(BloodUnit, Component.code, BloodGroup.code)
        .join(Component, Component.id == BloodUnit.component_id)
        .join(BloodGroup, BloodGroup.id == BloodUnit.blood_group_id)
        .where(BloodUnit.facility_id == facility_id)
        .order_by(BloodUnit.last_synced_at.desc(), BloodUnit.id)
        .limit(limit)
    )
    if updated_after:
        statement = statement.where(BloodUnit.last_synced_at >= updated_after)
    rows = db.execute(statement).all()
    return {
        "data": [
            {
                "id": unit.id,
                "source_system_ref": unit.source_system_ref,
                "din": unit.din,
                "component_code": component,
                "blood_group": group,
                "collected_at": unit.collected_at,
                "expires_at": unit.expires_at,
                "status": unit.status,
                "screening_status": unit.screening_status,
                "volume_ml": unit.volume_ml,
                "last_synced_at": unit.last_synced_at,
            }
            for unit, component, group in rows
        ],
        "count": len(rows),
        "limit": limit,
    }


@api_app.get("/v1/demand-events", tags=["Demand"])
def demand_events(
    facility_id: str,
    requested_after: datetime | None = None,
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    client: ApiClient = Depends(_client),
):
    _scope(client, "demand:read")
    _facility(db, client, facility_id)
    statement = (
        select(DemandEvent, Component.code, BloodGroup.code)
        .join(Component, Component.id == DemandEvent.component_id)
        .join(BloodGroup, BloodGroup.id == DemandEvent.blood_group_id)
        .where(DemandEvent.facility_id == facility_id)
        .order_by(DemandEvent.requested_at.desc(), DemandEvent.id)
        .limit(limit)
    )
    if requested_after:
        statement = statement.where(DemandEvent.requested_at >= requested_after)
    rows = db.execute(statement).all()
    return {
        "data": [
            {
                "id": event.id,
                "source_system_ref": event.source_system_ref,
                "requested_at": event.requested_at,
                "component_code": component,
                "blood_group": group,
                "units_requested": event.units_requested,
                "units_issued": event.units_issued,
                "urgency": event.urgency,
                "clinical_context": event.clinical_context,
                "outcome": event.outcome,
                "last_synced_at": event.last_synced_at,
            }
            for event, component, group in rows
        ],
        "count": len(rows),
        "limit": limit,
    }


@api_app.post("/v1/imports/canonical/preview", tags=["Imports"], status_code=201)
def canonical_preview(
    payload: CanonicalPreviewRequest,
    db: Session = Depends(get_db),
    client: ApiClient = Depends(_client),
):
    _scope(client, "imports:write")
    _facility(db, client, payload.facility_id)
    raw = json.dumps(payload.records, sort_keys=True, ensure_ascii=False)
    batch = integration_service.preview_records(
        db,
        _actor(client, payload.facility_id),
        organization_id=client.organization_id,
        facility_id=payload.facility_id,
        data_type=payload.data_type,
        mode="REST",
        filename=payload.source_name,
        content_type="application/json",
        raw_payload=raw,
        rows=payload.records,
    )
    return _batch_payload(batch)


@api_app.post("/v1/imports/fhir/preview", tags=["FHIR R4"], status_code=201)
def fhir_preview(
    payload: FhirPreviewRequest,
    db: Session = Depends(get_db),
    client: ApiClient = Depends(_client),
):
    _scope(client, "imports:write")
    _facility(db, client, payload.facility_id)
    try:
        preview = parse_bundle(payload.bundle)
    except FhirAdapterError as exc:
        _api_error("FHIR_BUNDLE_INVALID", str(exc), 422)
    raw = json.dumps(payload.bundle, sort_keys=True, ensure_ascii=False)
    batches = []
    for data_type, rows in (("INVENTORY", preview.inventory), ("DEMAND", preview.demand)):
        if not rows:
            continue
        batch = integration_service.preview_records(
            db,
            _actor(client, payload.facility_id),
            organization_id=client.organization_id,
            facility_id=payload.facility_id,
            data_type=data_type,
            mode="FHIR",
            filename=f"fhir-{data_type.lower()}-bundle.json",
            content_type="application/fhir+json",
            raw_payload=raw + f"\n# canonical-part:{data_type}",
            rows=rows,
        )
        batches.append(_batch_payload(batch))
    return {"batches": batches, "unsupported_entries": preview.unsupported}


@api_app.post("/v1/imports/hl7v2/preview", tags=["HL7 v2"], status_code=201)
def hl7_preview(
    payload: Hl7PreviewRequest,
    db: Session = Depends(get_db),
    client: ApiClient = Depends(_client),
):
    _scope(client, "imports:write")
    _facility(db, client, payload.facility_id)
    try:
        preview = parse_message(payload.message)
    except Hl7AdapterError as exc:
        _api_error("HL7_MESSAGE_INVALID", str(exc), 422)
    batch = integration_service.preview_records(
        db,
        _actor(client, payload.facility_id),
        organization_id=client.organization_id,
        facility_id=payload.facility_id,
        data_type=preview.data_type,
        mode="HL7V2",
        filename=f"hl7-{preview.message_type}.hl7",
        content_type="application/hl7-v2",
        raw_payload=payload.message,
        rows=preview.rows,
    )
    return {"message_type": preview.message_type, "batch": _batch_payload(batch)}


@api_app.get("/v1/imports/{batch_id}", tags=["Imports"])
def import_status(
    batch_id: str,
    include_rows: bool = Query(False),
    db: Session = Depends(get_db),
    client: ApiClient = Depends(_client),
):
    _scope(client, "imports:write")
    batch = integration_service.get_batch(db, client.organization_id, batch_id)
    _facility(db, client, batch.facility_id)
    result = _batch_payload(batch)
    if include_rows:
        rows = list(
            db.scalars(
                select(ImportRow)
                .where(ImportRow.batch_id == batch.id)
                .order_by(ImportRow.row_number)
                .limit(500)
            ).all()
        )
        result["rows"] = [
            {
                "row_number": row.row_number,
                "source_system_ref": row.source_system_ref,
                "status": row.status,
                "normalized": row.normalized_json,
                "errors": row.errors_json,
                "warnings": row.warnings_json,
                "entity_id": row.entity_id,
            }
            for row in rows
        ]
    return result


@api_app.post("/v1/imports/{batch_id}/commit", tags=["Imports"])
def import_commit(
    batch_id: str,
    payload: CommitRequest,
    db: Session = Depends(get_db),
    client: ApiClient = Depends(_client),
):
    _scope(client, "imports:write")
    if not payload.confirm:
        _api_error("COMMIT_CONFIRMATION_REQUIRED", "Set confirm=true to commit valid rows.", 422)
    batch = integration_service.get_batch(db, client.organization_id, batch_id)
    _facility(db, client, batch.facility_id)
    committed = integration_service.commit_batch(
        db,
        _actor(client, batch.facility_id),
        organization_id=client.organization_id,
        batch_id=batch.id,
    )
    return _batch_payload(committed)


@api_app.get("/v1/provenance/{entity_type}/{entity_id}", tags=["Provenance"])
def provenance(
    entity_type: str,
    entity_id: str,
    db: Session = Depends(get_db),
    client: ApiClient = Depends(_client),
):
    records = list(
        db.scalars(
            select(SourceProvenance)
            .where(
                SourceProvenance.organization_id == client.organization_id,
                SourceProvenance.entity_type == entity_type,
                SourceProvenance.entity_id == entity_id,
                SourceProvenance.facility_id.in_(_allowed_facilities(db, client) or ["__none__"]),
            )
            .order_by(SourceProvenance.last_seen_at.desc())
        ).all()
    )
    return {
        "data": [
            {
                "source_mode": row.source_mode,
                "source_system_ref": row.source_system_ref,
                "payload_hash": row.payload_hash,
                "first_seen_at": row.first_seen_at,
                "last_seen_at": row.last_seen_at,
                "version_count": row.version_count,
                "batch_id": row.batch_id,
            }
            for row in records
        ]
    }
