"""Inventory: the shelf, and what stands behind every bag on it.

The list answers "what do we hold". The three pages beneath it answer the
questions that matter when something has gone wrong.

* **Unit detail** — the backward trace. One bag, its whole chain: donor,
  screening, collection, plate and kit lot, separation, fridge.
* **Donor recall** — the forward trace. One donor, every unit they have given
  and where each one is now.
* **Storage** — the fridges themselves, and which units were in one while it was
  out of range.

The list route lives here rather than in `facility.py` so that a page and the
actions taken from it sit together.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Permission, can
from db.models import (
    BloodGroup,
    BloodUnit,
    Component,
    StorageLocation,
    TemperatureLog,
)
from i18n.t import t
from services import discard as discard_service
from services import traceability
from services.audit import Actor, PermissionDenied, ServiceError
from services.common import DEMO_DATETIME
from web.deps import Principal, get_db, require_permission, require_principal
from web.routers.facility import ENABLED_NAV, nav_counts
from web.templating import current_lang, flash, render

router = APIRouter(
    prefix="/app/inventory",
    dependencies=[Depends(require_permission(Permission.VIEW_LOCAL_INVENTORY))],
)

STATUSES = (
    "AVAILABLE",
    "RESERVED",
    "CROSSMATCHED",
    "QUARANTINE",
    "IN_TRANSIT",
    "MISSING_IN_TRANSIT",
)

# How often the probes log. Kept in step with `datagen.storage`, and used to
# decide when two out-of-range readings belong to the same excursion.
SAMPLE_INTERVAL_MINUTES = 30


def _actor(principal: Principal, request: Request) -> Actor:
    return Actor.from_principal(principal, request)


def _page(request: Request, principal: Principal, db: Session, **kwargs):
    return render(
        request,
        principal=principal,
        db=db,
        nav_counts=nav_counts(db, principal),
        enabled_nav=ENABLED_NAV,
        **kwargs,
    )


def _crumbs(lang: str, *extra: dict) -> list[dict]:
    return [
        {"label": t("nav.dashboard", language=lang), "url": "/app/dashboard"},
        {"label": t("nav.inventory", language=lang), "url": "/app/inventory"},
        *extra,
    ]


@router.get("")
def stock(
    request: Request,
    component: str = Query(""),
    group: str = Query(""),
    status: str = Query("AVAILABLE"),
    expiring: str = Query(""),
    store: str = Query(""),
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    lang = current_lang(request)
    facility_id = principal.facility_id

    components = db.execute(
        select(Component.code, Component.name_en).order_by(Component.id)
    ).all()
    groups = db.execute(select(BloodGroup.code).order_by(BloodGroup.id)).all()

    units: list = []
    stores: list = []
    total = 0

    if facility_id:
        statement = (
            select(
                BloodUnit.id,
                BloodUnit.din,
                Component.code.label("component_code"),
                BloodGroup.code.label("group_code"),
                BloodUnit.volume_ml,
                BloodUnit.collected_at,
                BloodUnit.expires_at,
                BloodUnit.status,
                BloodUnit.screening_status,
                BloodUnit.is_leucodepleted,
                BloodUnit.is_irradiated,
                BloodUnit.cold_chain_breach_count,
                StorageLocation.name.label("store_name"),
                StorageLocation.is_out_of_range.label("store_alarm"),
            )
            .join(Component, Component.id == BloodUnit.component_id)
            .join(BloodGroup, BloodGroup.id == BloodUnit.blood_group_id)
            .outerjoin(
                StorageLocation,
                StorageLocation.id == BloodUnit.storage_location_id,
            )
            .where(
                BloodUnit.facility_id == facility_id,
                BloodUnit.expires_at > DEMO_DATETIME,
            )
        )

        if status:
            statement = statement.where(BloodUnit.status == status)

        if component:
            statement = statement.where(Component.code == component)

        if group:
            statement = statement.where(BloodGroup.code == group)

        if store:
            statement = statement.where(BloodUnit.storage_location_id == store)

        if expiring == "72h":
            statement = statement.where(
                BloodUnit.expires_at <= DEMO_DATETIME + timedelta(hours=72)
            )
        elif expiring == "7d":
            statement = statement.where(
                BloodUnit.expires_at <= DEMO_DATETIME + timedelta(days=7)
            )

        total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0

        # First expiry first out: the order stock must always be read in. The
        # shelf can contain hundreds of units, so keep the operational view
        # digestible without hiding any record behind a hard result cap.
        page_size = 25
        pages = max(1, (int(total) + page_size - 1) // page_size)
        page = max(1, min(int(page), pages))
        units = db.execute(
            statement.order_by(BloodUnit.expires_at)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()

        stores = db.execute(
            select(
                StorageLocation.id,
                StorageLocation.name,
                StorageLocation.location_type,
                StorageLocation.last_temp_c,
                StorageLocation.target_temp_min_c,
                StorageLocation.target_temp_max_c,
                StorageLocation.is_out_of_range,
                func.count(BloodUnit.id).label("held"),
            )
            .outerjoin(
                BloodUnit,
                (BloodUnit.storage_location_id == StorageLocation.id)
                & BloodUnit.status.in_(STATUSES),
            )
            .where(
                StorageLocation.facility_id == facility_id,
                StorageLocation.is_active.is_(True),
            )
            .group_by(StorageLocation.id)
            .order_by(StorageLocation.location_type, StorageLocation.name)
        ).all()

    return _page(
        request,
        principal,
        db,
        template="app/inventory.html",
        context={
            "units": units,
            "total": total,
            "shown": len(units),
            "page": page,
            "pages": pages if facility_id else 1,
            "components": components,
            "groups": [row[0] for row in groups],
            "stores": stores,
            "wastage": (
                discard_service.wastage_summary(db, facility_id)
                if facility_id
                else None
            ),
            "filters": {
                "component": component,
                "group": group,
                "status": status,
                "expiring": expiring,
                "store": store,
            },
            "statuses": list(STATUSES),
            "now": DEMO_DATETIME,
        },
        page_title=t("nav.inventory", language=lang),
        breadcrumbs=_crumbs(lang),
    )


@router.get("/storage")
def storage(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    """The fridges, and how they have behaved."""

    lang = current_lang(request)
    facility_id = principal.facility_id
    since = DEMO_DATETIME - timedelta(days=14)

    rows: list = []

    if facility_id:
        locations = db.scalars(
            select(StorageLocation)
            .where(
                StorageLocation.facility_id == facility_id,
                StorageLocation.is_active.is_(True),
            )
            .order_by(StorageLocation.location_type, StorageLocation.name)
        ).all()

        held = dict(
            db.execute(
                select(BloodUnit.storage_location_id, func.count())
                .where(BloodUnit.status.in_(STATUSES))
                .group_by(BloodUnit.storage_location_id)
            ).all()
        )

        # Events, not readings. Counting readings would put "2 breaches" on a
        # card whose own detail page says "1 excursion", because a half-hour
        # door-open is sampled twice — and a user who notices that stops
        # trusting both numbers.
        breaches: dict[str, int] = {}
        stamps: dict[str, list] = {}

        for row in db.execute(
            select(
                TemperatureLog.storage_location_id, TemperatureLog.recorded_at
            )
            .where(
                TemperatureLog.is_out_of_range.is_(True),
                TemperatureLog.recorded_at >= since,
            )
            .order_by(
                TemperatureLog.storage_location_id, TemperatureLog.recorded_at
            )
        ).all():
            stamps.setdefault(row.storage_location_id, []).append(row.recorded_at)

        for store_id, times in stamps.items():
            # Readings are sampled at a fixed interval, so a gap wider than one
            # interval means the store recovered and went out again.
            gap = timedelta(minutes=SAMPLE_INTERVAL_MINUTES + 1)
            breaches[store_id] = 1 + sum(
                1
                for earlier, later in zip(times, times[1:])
                if later - earlier > gap
            )

        for location in locations:
            # A sparkline of the last 48 readings — a day of history, enough to
            # see a door left open without loading a fortnight of points.
            trace = db.execute(
                select(TemperatureLog.recorded_at, TemperatureLog.temperature_c)
                .where(TemperatureLog.storage_location_id == location.id)
                .order_by(TemperatureLog.recorded_at.desc())
                .limit(48)
            ).all()

            rows.append(
                {
                    "location": location,
                    "held": held.get(location.id, 0),
                    "breaches": breaches.get(location.id, 0),
                    "trace": list(reversed([float(r.temperature_c) for r in trace])),
                }
            )

    return _page(
        request,
        principal,
        db,
        template="app/storage.html",
        context={"rows": rows, "days": 14, "now": DEMO_DATETIME},
        page_title=t("ops.storage_cold_chain", language=lang),
        breadcrumbs=_crumbs(
            lang,
            {"label": t("ops.storage", language=lang), "url": "/app/inventory/storage"},
        ),
    )


@router.get("/storage/{location_id}")
def store_detail(
    location_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    """One store: its readings, its excursions, and what was in it at the time."""

    lang = current_lang(request)
    since = DEMO_DATETIME - timedelta(days=14)

    location = db.scalars(
        select(StorageLocation).where(
            StorageLocation.id == location_id,
            StorageLocation.facility_id == principal.facility_id,
        )
    ).first()

    if location is None:
        flash(
            request,
            t("ops.storage_not_at_facility", language=current_lang(request)),
            "error",
        )
        return RedirectResponse("/app/inventory/storage", status_code=303)

    readings = db.execute(
        select(
            TemperatureLog.recorded_at,
            TemperatureLog.temperature_c,
            TemperatureLog.is_out_of_range,
            TemperatureLog.source,
        )
        .where(
            TemperatureLog.storage_location_id == location_id,
            TemperatureLog.recorded_at >= since,
        )
        .order_by(TemperatureLog.recorded_at)
    ).all()

    excursions = _group_excursions(readings)

    # The units that were in it while it was out of range. This is the point of
    # the page: an excursion nobody can tie to stock is a number, not a finding.
    exposed: list = []

    if excursions:
        widest_start = min(item["start"] for item in excursions)
        widest_end = max(item["end"] for item in excursions)
        exposed = traceability.units_in_store_during(
            db, location_id=location_id, start=widest_start, end=widest_end
        )

    contents = db.execute(
        select(func.count())
        .select_from(BloodUnit)
        .where(
            BloodUnit.storage_location_id == location_id,
            BloodUnit.status.in_(STATUSES),
        )
    ).scalar()

    return _page(
        request,
        principal,
        db,
        template="app/store_detail.html",
        context={
            "location": location,
            "readings": readings,
            "excursions": excursions,
            "exposed": exposed[:100],
            "exposed_total": len(exposed),
            "held": contents or 0,
            "days": 14,
            "now": DEMO_DATETIME,
        },
        page_title=location.name,
        breadcrumbs=_crumbs(
            lang,
            {"label": t("ops.storage", language=lang), "url": "/app/inventory/storage"},
            {
                "label": location.name,
                "url": f"/app/inventory/storage/{location_id}",
            },
        ),
    )


def _group_excursions(readings) -> list[dict]:
    """Collapse consecutive out-of-range readings into one event each.

    A four-hour compressor failure logged every thirty minutes is eight rows in
    the table and one thing that happened. Listing it eight times makes a page
    that cannot be read and a count that cannot be trusted.
    """

    events: list[dict] = []
    current: dict | None = None

    for row in readings:
        if row.is_out_of_range:
            if current is None:
                current = {
                    "start": row.recorded_at,
                    "end": row.recorded_at,
                    "peak": float(row.temperature_c),
                    "source": row.source,
                    "readings": 1,
                }
            else:
                current["end"] = row.recorded_at
                current["readings"] += 1
                current["peak"] = max(current["peak"], float(row.temperature_c))
        elif current is not None:
            events.append(current)
            current = None

    if current is not None:
        events.append(current)

    for event in events:
        minutes = (event["end"] - event["start"]).total_seconds() / 60.0
        # A single reading is not a zero-length event: it stands for the
        # interval it was sampled over.
        event["minutes"] = max(30.0, minutes)

    return sorted(events, key=lambda item: item["start"], reverse=True)


@router.get("/unit/{unit_id}")
def unit_detail(
    unit_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    """One bag, and everything behind it."""

    lang = current_lang(request)
    actor = _actor(principal, request)

    trace = traceability.trace_unit(db, actor, unit_id=unit_id)

    if trace is None:
        flash(
            request,
            t("ops.unit_not_at_facility", language=current_lang(request)),
            "error",
        )
        return RedirectResponse("/app/inventory", status_code=303)

    unit = trace["unit"]

    return _page(
        request,
        principal,
        db,
        template="app/unit_detail.html",
        context={
            **trace,
            "discard_reasons": discard_service.reason_choices(),
            "can_discard": can(principal.user, Permission.DISCARD_UNIT),
            "discardable": unit.status
            not in discard_service.FINISHED + discard_service.COMMITTED,
            "now": DEMO_DATETIME,
        },
        page_title=unit.din,
        breadcrumbs=_crumbs(
            lang, {"label": unit.din, "url": f"/app/inventory/unit/{unit_id}"}
        ),
    )


@router.post("/unit/{unit_id}/discard")
def discard_unit(
    unit_id: str,
    request: Request,
    reason: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    try:
        unit = discard_service.discard(
            db,
            _actor(principal, request),
            unit_id=unit_id,
            reason=reason,
            note=note or None,
        )
    except (ServiceError, PermissionDenied) as error:
        flash(request, error.message, "error")
        return RedirectResponse(f"/app/inventory/unit/{unit_id}", status_code=303)

    flash(
        request,
        t(
            "ops.unit_discarded_flash",
            language=current_lang(request),
            din=unit.din,
        ),
        "success",
    )

    return RedirectResponse(f"/app/inventory/unit/{unit_id}", status_code=303)


@router.get("/recall/{donor_id}")
def donor_recall(
    donor_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    """Every unit one donor has given, and where each one is now."""

    lang = current_lang(request)

    trace = traceability.trace_donor(db, _actor(principal, request), donor_id=donor_id)

    if trace is None:
        flash(
            request,
            t("ops.donor_not_at_facility", language=current_lang(request)),
            "error",
        )
        return RedirectResponse("/app/donors", status_code=303)

    donor = trace["donor"]

    return _page(
        request,
        principal,
        db,
        template="app/donor_recall.html",
        context={**trace, "now": DEMO_DATETIME},
        page_title=t(
            "ops.recall_title", language=lang, code=donor.donor_code
        ),
        breadcrumbs=_crumbs(
            lang,
            {
                "label": t("ops.recall_label", language=lang, code=donor.donor_code),
                "url": f"/app/inventory/recall/{donor_id}",
            },
        ),
    )
