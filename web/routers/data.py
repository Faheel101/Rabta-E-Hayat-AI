"""Data & Integrations workspace: preview, quarantine, commit and provenance."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.auth import CurrentUser, Permission, Role, can, can_open_page
from i18n.t import t
from services import integration_service
from services.audit import Actor, ServiceError
from web.deps import Principal, get_db, require_principal
from web.routers.facility import ENABLED_NAV, nav_counts
from web.templating import current_lang, flash, render

router = APIRouter(prefix="/data")


def _guard(principal: Principal) -> None:
    try:
        subject = principal.role_subject(role=Role(principal.role))
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Unknown role") from exc
    if not can_open_page(subject, "data") or not can(subject, Permission.MANAGE_INTEGRATIONS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your role does not permit the data integration workspace",
        )


def _actor(principal: Principal, request: Request) -> Actor:
    return Actor.from_principal(principal, request)


def _render_workspace(
    request: Request,
    principal: Principal,
    db: Session,
    *,
    issued_key: str | None = None,
    issued_client=None,
):
    payload = integration_service.workspace(db, principal.organization_id)
    if not principal.is_group_user:
        upload_facilities = [
            item for item in payload["facilities"] if item.id == principal.facility_id
        ]
    else:
        upload_facilities = payload["facilities"]
    return render(
        request,
        "data/index.html",
        {
            **payload,
            "upload_facilities": upload_facilities,
            "issued_key": issued_key,
            "issued_client": issued_client,
        },
        principal=principal,
        db=db,
        nav_counts=nav_counts(db, principal),
        enabled_nav=ENABLED_NAV,
        page_title=t("data.title", language=current_lang(request)),
    )


@router.get("")
def workspace(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    return _render_workspace(request, principal, db)


@router.post("/imports/preview")
async def preview_upload(
    request: Request,
    facility_id: str = Form(...),
    data_type: str = Form(...),
    source_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        payload = await source_file.read(integration_service.MAX_PAYLOAD_BYTES + 1)
        batch = integration_service.preview_csv(
            db,
            _actor(principal, request),
            organization_id=principal.organization_id,
            facility_id=facility_id,
            data_type=data_type,
            filename=source_file.filename or "manual-upload.csv",
            payload=payload,
        )
    except ServiceError as exc:
        flash(request, exc.message, "critical")
        return RedirectResponse("/data", status_code=303)
    return RedirectResponse(f"/data/imports/{batch.id}", status_code=303)


@router.get("/imports/{batch_id}")
def import_detail(
    request: Request,
    batch_id: str,
    page: int = Query(1),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        payload = integration_service.batch_detail(
            db, principal.organization_id, batch_id, page=page
        )
    except ServiceError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    if not principal.is_group_user:
        principal.require_own_facility(payload["batch"].facility_id)
    return render(
        request,
        "data/import_detail.html",
        payload,
        principal=principal,
        db=db,
        nav_counts=nav_counts(db, principal),
        enabled_nav=ENABLED_NAV,
        page_title=t("data.batch_title", language=current_lang(request)),
        breadcrumbs=[
            {"label": t("data.title", language=current_lang(request)), "url": "/data"},
            {"label": payload["batch"].filename or payload["batch"].id[:8], "url": "#"},
        ],
    )


@router.post("/imports/{batch_id}/mapping")
async def update_mapping(
    request: Request,
    batch_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    form = await request.form()
    mapping = {
        key.removeprefix("map_"): str(value)
        for key, value in form.multi_items()
        if key.startswith("map_") and str(value)
    }
    try:
        integration_service.remap_batch(
            db,
            _actor(principal, request),
            organization_id=principal.organization_id,
            batch_id=batch_id,
            mapping=mapping,
        )
        flash(request, t("data.mapping_saved", language=current_lang(request)), "safe")
    except ServiceError as exc:
        flash(request, exc.message, "critical")
    return RedirectResponse(f"/data/imports/{batch_id}", status_code=303)


@router.post("/imports/{batch_id}/commit")
def commit_import(
    request: Request,
    batch_id: str,
    confirmation: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    if confirmation != "COMMIT VALID ROWS":
        flash(request, t("data.commit_phrase_error", language=current_lang(request)), "critical")
        return RedirectResponse(f"/data/imports/{batch_id}", status_code=303)
    try:
        batch = integration_service.commit_batch(
            db,
            _actor(principal, request),
            organization_id=principal.organization_id,
            batch_id=batch_id,
        )
        flash(
            request,
            t(
                "data.commit_success",
                language=current_lang(request),
                count=batch.ingested_rows,
            ),
            "safe",
        )
    except ServiceError as exc:
        flash(request, exc.message, "critical")
    return RedirectResponse(f"/data/imports/{batch_id}", status_code=303)


@router.get("/imports/{batch_id}/errors.csv")
def error_report(
    batch_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        payload = integration_service.error_report_csv(
            db, principal.organization_id, batch_id
        )
    except ServiceError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return Response(
        payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="rabta-import-{batch_id[:8]}-errors.csv"'},
    )


@router.get("/templates/{data_type}.csv")
def download_template(
    data_type: str,
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        payload = integration_service.template_csv(data_type)
    except ServiceError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    value = data_type.lower()
    return Response(
        payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="rabta-{value}-template.csv"'},
    )


@router.post("/issues/{issue_id}/resolve")
def resolve_issue(
    request: Request,
    issue_id: str,
    note: str = Form(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        integration_service.resolve_issue(
            db,
            _actor(principal, request),
            organization_id=principal.organization_id,
            issue_id=issue_id,
            note=note,
        )
        flash(request, t("data.issue_resolved", language=current_lang(request)), "safe")
    except ServiceError as exc:
        flash(request, exc.message, "critical")
    return RedirectResponse("/data#reconciliation", status_code=303)


@router.post("/api-clients")
async def create_api_client(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    form = await request.form()
    try:
        client, secret = integration_service.create_api_client(
            db,
            _actor(principal, request),
            organization_id=principal.organization_id,
            name=str(form.get("name") or ""),
            scopes=[str(value) for value in form.getlist("scopes")],
            facility_ids=[str(value) for value in form.getlist("facility_ids")],
        )
    except ServiceError as exc:
        flash(request, exc.message, "critical")
        return RedirectResponse("/data#api", status_code=303)
    return _render_workspace(
        request,
        principal,
        db,
        issued_key=secret,
        issued_client=client,
    )


@router.post("/api-clients/{client_id}/revoke")
def revoke_api_client(
    request: Request,
    client_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    _guard(principal)
    try:
        integration_service.revoke_api_client(
            db,
            _actor(principal, request),
            organization_id=principal.organization_id,
            client_id=client_id,
        )
        flash(request, t("data.client_revoked", language=current_lang(request)), "safe")
    except ServiceError as exc:
        flash(request, exc.message, "critical")
    return RedirectResponse("/data#api", status_code=303)


@router.get("/examples/fhir-bundle.json")
def fhir_example(principal: Principal = Depends(require_principal)):
    _guard(principal)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {
                "resource": {
                    "resourceType": "BiologicallyDerivedProduct",
                    "id": "example-unit-001",
                    "identifier": [{"system": "urn:rabta:bbmis", "value": "FHIR-UNIT-001"}],
                    "productCode": {"coding": [{"system": "urn:rabta:component", "code": "PRBC"}]},
                    "collection": {"collectedDateTime": (now - timedelta(days=1)).isoformat()},
                    "expirationDate": (now + timedelta(days=34)).isoformat(),
                    "status": "AVAILABLE",
                    "extension": [
                        {"url": "https://rabta.pk/fhir/StructureDefinition/blood-group", "valueCode": "O+"},
                        {"url": "https://rabta.pk/fhir/StructureDefinition/screening-status", "valueCode": "PASSED"},
                        {"url": "https://rabta.pk/fhir/StructureDefinition/volume-ml", "valueInteger": 300},
                    ],
                }
            }
        ],
    }
    return JSONResponse(
        payload,
        headers={"Content-Disposition": 'attachment; filename="rabta-fhir-r4-example.json"'},
    )


@router.get("/examples/hl7v2-message.txt")
def hl7_example(principal: Principal = Depends(require_principal)):
    _guard(principal)
    payload = (
        "MSH|^~\\&|DEMO_HIS|JHL|RABTA|PUNJAB|20260816110000||ORM^O01|MSG00001|P|2.5\r"
        "ZRH|HL7-REQ-001|20260816103000|PRBC|O+|2|1|URGENT|TRAUMA|PARTIAL\r"
    )
    return Response(
        payload,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="rabta-hl7v2-example.hl7"'},
    )
