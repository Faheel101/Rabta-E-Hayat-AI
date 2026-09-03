"""Transfer plan reads and the approve / reject / modify writes (spec §8.4).

Spec §2.2: this system never moves blood without human approval, so these writes
are the only place a recommendation becomes an instruction. Every one of them
appends to audit_log — acceptance criterion 7 requires a transfer to be
approvable, dispatchable, trackable and receivable with a complete trail, and an
append-only audit log is called non-negotiable for a clinical supply system in
§4.2.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from app.auth import Permission
from core import config
from core import policy
from core.clock import as_utc
from db.models import (
    AuditLog,
    BloodGroup,
    BloodUnit,
    Component,
    EmergencyIncident,
    Facility,
    Organization,
    StorageLocation,
    Transfer,
    TransferPlan,
)
from services.common import DEMO_DATETIME, cached, clear_caches, read_sql
from services.audit import Actor, ServiceError, audited, require, snapshot

COST_PER_UNIT = float(config.get("impact.cost_per_unit_pkr", 15000))

PENDING = "RECOMMENDED"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
DISPATCHED = "DISPATCHED"
IN_TRANSIT = "IN_TRANSIT"
RECEIVED = "RECEIVED"
FAILED_COLD_CHAIN = "FAILED_COLD_CHAIN"
CANCELLED = "CANCELLED"

ACTIVE_TRANSFER_STATUSES = {PENDING, APPROVED, DISPATCHED, IN_TRANSIT}
SEAL_STATUSES = {"INTACT", "BROKEN", "MISSING"}

REJECTION_REASONS = [
    "no_transport",
    "disagree_with_forecast",
    "unit_needed_locally",
    "receiving_facility_declined",
    "other",
]


@cached()
def current_plan() -> dict:
    frame = read_sql(
        select(TransferPlan)
        .where(TransferPlan.status == "GENERATED")
        .order_by(TransferPlan.created_at.desc())
    )

    if frame.empty:
        frame = read_sql(select(TransferPlan).order_by(TransferPlan.created_at.desc()))

    if frame.empty:
        return {}

    row = frame.iloc[0].to_dict()
    row["created_at"] = pd.to_datetime(row["created_at"], utc=True)

    return row


@cached()
def transfers(facility_ids: tuple[str, ...] | None = None) -> pd.DataFrame:
    """Plan rows with names, codes and cold-chain facts resolved."""

    statement = select(
        Transfer.id,
        Transfer.plan_id,
        Transfer.from_facility_id,
        Transfer.to_facility_id,
        Transfer.component_id,
        Transfer.blood_group_id,
        Transfer.recipient_group_id,
        Transfer.preference_rank,
        Transfer.units,
        Transfer.status,
        Transfer.unit_ids,
        Transfer.est_travel_minutes,
        Transfer.distance_km,
        Transfer.transport_mode,
        Transfer.rationale_en,
        Transfer.projected_units_saved,
        Transfer.approved_by,
        Transfer.approved_at,
        Transfer.rejection_reason,
        Transfer.rejection_note,
        Transfer.created_at,
        Component.code.label("component_code"),
        Component.max_transport_hours,
        Component.requires_agitation,
        Component.storage_temp_min_c,
        Component.storage_temp_max_c,
    ).join(Component, Component.id == Transfer.component_id)

    plan = current_plan()
    if plan.get("id"):
        statement = statement.where(Transfer.plan_id == plan["id"])

    frame = read_sql(statement)

    if frame.empty:
        return frame

    from services.facility_service import blood_groups, facilities

    names = facilities().set_index("facility_id")["name_en"].to_dict()
    group_codes = blood_groups().set_index("blood_group_id")["code"].to_dict()

    frame["from_name"] = frame["from_facility_id"].map(names)
    frame["to_name"] = frame["to_facility_id"].map(names)
    frame["group_code"] = frame["blood_group_id"].map(group_codes)
    frame["recipient_group_code"] = frame["recipient_group_id"].map(group_codes)
    frame["created_at"] = pd.to_datetime(frame["created_at"], utc=True)

    if facility_ids:
        ids = list(facility_ids)
        frame = frame[
            frame["from_facility_id"].isin(ids) | frame["to_facility_id"].isin(ids)
        ]

    return frame.reset_index(drop=True)


@cached()
def plan_impact(facility_ids: tuple[str, ...] | None = None) -> dict:
    """Impact of the plan as stored, plus the solver's own diagnostics.

    The stored figures are recomputed from persisted rows by the optimizer, so
    what this returns always describes the plan the user can see.
    """

    plan = current_plan()
    rows = transfers(facility_ids)

    parameters = plan.get("parameters_json") or {}
    solver = parameters.get("solver") or {}

    if rows.empty:
        return {"plan": plan, "solver": solver, "empty": True}

    return {
        "plan": plan,
        "solver": solver,
        "empty": False,
        "transfers": int(len(rows)),
        "units": int(rows["units"].sum()),
        "shipments": int(
            rows.groupby(["from_facility_id", "to_facility_id"]).ngroups
        ),
        "distance_km": float(
            rows.drop_duplicates(["from_facility_id", "to_facility_id"])[
                "distance_km"
            ].sum()
        ),
        "facilities_involved": int(
            pd.concat([rows["from_facility_id"], rows["to_facility_id"]]).nunique()
        ),
        "value_protected": float(rows["units"].sum()) * COST_PER_UNIT,
        "pending": int((rows["status"] == PENDING).sum()),
        "approved": int((rows["status"] == APPROVED).sum()),
        "rejected": int((rows["status"] == REJECTED).sum()),
        "shortages_averted": solver.get("shortages_averted"),
        "units_rescued": solver.get("units_rescued"),
        "unmet_demand": solver.get("unmet_demand"),
        "total_deficit": solver.get("total_deficit"),
        "optimality_gap": solver.get("optimality_gap"),
        "solver_status": solver.get("status"),
    }


@cached()
def transfer_units(transfer_id: str) -> pd.DataFrame:
    """The physical bags assigned to a transfer, earliest expiry first."""

    frame = read_sql(select(Transfer.unit_ids).where(Transfer.id == transfer_id))

    if frame.empty:
        return pd.DataFrame()

    unit_ids = frame.iloc[0]["unit_ids"] or []

    if not unit_ids:
        return pd.DataFrame()

    units = read_sql(
        select(
            BloodUnit.id,
            BloodUnit.din,
            BloodUnit.isbt_product_code,
            BloodUnit.expires_at,
            BloodUnit.collected_at,
            BloodUnit.volume_ml,
            BloodUnit.is_leucodepleted,
            BloodUnit.is_irradiated,
        ).where(BloodUnit.id.in_(list(unit_ids)))
    )

    if units.empty:
        return units

    units["expires_at"] = pd.to_datetime(units["expires_at"], utc=True)
    units["collected_at"] = pd.to_datetime(units["collected_at"], utc=True)
    units["days_left"] = (
        units["expires_at"] - pd.Timestamp(DEMO_DATETIME)
    ).dt.total_seconds() / 86400.0

    return units.sort_values("expires_at").reset_index(drop=True)


@cached()
def rescued_units_mtd(facility_ids: tuple[str, ...]) -> int:
    """Units approved for movement out of these facilities, month to date."""

    if not facility_ids:
        return 0

    month_start = DEMO_DATETIME.replace(day=1, hour=0, minute=0, second=0)

    frame = read_sql(
        select(func.coalesce(func.sum(Transfer.units), 0)).where(
            Transfer.status.in_([APPROVED, "DISPATCHED", "IN_TRANSIT", "RECEIVED"]),
            Transfer.from_facility_id.in_(list(facility_ids)),
            Transfer.approved_at >= month_start,
        )
    )

    return int(frame.iloc[0, 0]) if not frame.empty else 0


def plan_workspace(
    db: Session,
    facility_ids: list[str],
    *,
    status_filter: str = "",
    direction: str = "",
    query: str = "",
    view: str = "current",
    page: int = 1,
    page_size: int = 40,
) -> dict:
    """Tenant-scoped plan queue for the operational web application."""

    scope = set(facility_ids)
    if not scope:
        return {
            "plan": None,
            "rows": [],
            "summary": {"transfers": 0, "units": 0, "routes": 0, "pending": 0},
            "total": 0,
            "page": 1,
            "pages": 1,
        }

    plan = db.scalars(
        select(TransferPlan)
        .where(TransferPlan.status == "GENERATED")
        .order_by(TransferPlan.created_at.desc())
    ).first()
    if plan is None:
        plan = db.scalars(select(TransferPlan).order_by(TransferPlan.created_at.desc())).first()

    Source = aliased(Facility)
    Destination = aliased(Facility)
    DonorGroup = aliased(BloodGroup)
    RecipientGroup = aliased(BloodGroup)
    visible = or_(
        Transfer.from_facility_id.in_(scope),
        Transfer.to_facility_id.in_(scope),
    )
    if plan is not None:
        if view == "history":
            visible = visible & (Transfer.plan_id != plan.id)
        else:
            visible = visible & (Transfer.plan_id == plan.id)
    columns = (
        Transfer,
        Source.name_en.label("source_name"),
        Source.code.label("source_code"),
        Destination.name_en.label("destination_name"),
        Destination.code.label("destination_code"),
        Component.code.label("component_code"),
        Component.name_en.label("component_name"),
        Component.storage_temp_min_c.label("temp_min"),
        Component.storage_temp_max_c.label("temp_max"),
        Component.requires_agitation.label("requires_agitation"),
        DonorGroup.code.label("group_code"),
        RecipientGroup.code.label("recipient_group_code"),
    )
    statement = (
        select(*columns)
        .join(Source, Source.id == Transfer.from_facility_id)
        .join(Destination, Destination.id == Transfer.to_facility_id)
        .join(Component, Component.id == Transfer.component_id)
        .join(DonorGroup, DonorGroup.id == Transfer.blood_group_id)
        .outerjoin(RecipientGroup, RecipientGroup.id == Transfer.recipient_group_id)
        .where(visible)
    )
    count_statement = select(func.count()).select_from(Transfer).where(visible)

    if status_filter:
        statement = statement.where(Transfer.status == status_filter)
        count_statement = count_statement.where(Transfer.status == status_filter)
    if direction == "outbound":
        statement = statement.where(Transfer.from_facility_id.in_(scope))
        count_statement = count_statement.where(Transfer.from_facility_id.in_(scope))
    elif direction == "inbound":
        statement = statement.where(Transfer.to_facility_id.in_(scope))
        count_statement = count_statement.where(Transfer.to_facility_id.in_(scope))
    search = query.strip()
    if search:
        condition = or_(
            Transfer.tracking_code.ilike(f"%{search}%"),
            Source.name_en.ilike(f"%{search}%"),
            Destination.name_en.ilike(f"%{search}%"),
            Component.code.ilike(f"%{search}%"),
            DonorGroup.code.ilike(f"%{search}%"),
        )
        statement = statement.where(condition)
        # The count query needs the same joins only when textual search applies.
        count_statement = (
            select(func.count())
            .select_from(Transfer)
            .join(Source, Source.id == Transfer.from_facility_id)
            .join(Destination, Destination.id == Transfer.to_facility_id)
            .join(Component, Component.id == Transfer.component_id)
            .join(DonorGroup, DonorGroup.id == Transfer.blood_group_id)
            .where(visible, condition)
        )
        if status_filter:
            count_statement = count_statement.where(Transfer.status == status_filter)
        if direction == "outbound":
            count_statement = count_statement.where(Transfer.from_facility_id.in_(scope))
        elif direction == "inbound":
            count_statement = count_statement.where(Transfer.to_facility_id.in_(scope))

    total = int(db.scalar(count_statement) or 0)
    page_size = max(10, min(100, int(page_size)))
    pages = max(1, math.ceil(total / page_size))
    page = max(1, min(int(page), pages))
    status_order = {
        PENDING: 0,
        APPROVED: 1,
        DISPATCHED: 2,
        IN_TRANSIT: 3,
        FAILED_COLD_CHAIN: 4,
        RECEIVED: 5,
        REJECTED: 6,
        CANCELLED: 7,
    }
    results = db.execute(
        statement.order_by(
            Transfer.created_at.desc(),
            Source.name_en,
            Destination.name_en,
            Component.id,
            DonorGroup.id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    rows = []
    for result in results:
        row = dict(result._mapping)
        record = row.pop("Transfer")
        row["record"] = record
        row["direction"] = (
            "outbound"
            if record.from_facility_id in scope
            and record.to_facility_id not in scope
            else "inbound"
            if record.to_facility_id in scope
            and record.from_facility_id not in scope
            else "internal"
        )
        row["status_rank"] = status_order.get(record.status, 99)
        rows.append(row)
    rows.sort(key=lambda row: (row["status_rank"], row["source_name"], row["destination_name"]))

    summary_rows = db.execute(
        select(Transfer.status, func.count(), func.coalesce(func.sum(Transfer.units), 0))
        .where(visible)
        .group_by(Transfer.status)
    ).all()
    by_status = {item[0]: {"count": int(item[1]), "units": int(item[2])} for item in summary_rows}
    routes = db.execute(
        select(Transfer.from_facility_id, Transfer.to_facility_id).where(visible).distinct()
    ).all()
    return {
        "plan": plan,
        "rows": rows,
        "summary": {
            "transfers": sum(item["count"] for item in by_status.values()),
            "units": sum(item["units"] for item in by_status.values()),
            "routes": len(routes),
            "pending": by_status.get(PENDING, {}).get("count", 0),
            "approved": by_status.get(APPROVED, {}).get("count", 0),
            "in_transit": by_status.get(IN_TRANSIT, {}).get("count", 0),
            "exceptions": by_status.get(FAILED_COLD_CHAIN, {}).get("count", 0),
            "received": by_status.get(RECEIVED, {}).get("count", 0),
        },
        "by_status": by_status,
        "total": total,
        "page": page,
        "pages": pages,
    }


def transfer_workspace(db: Session, facility_ids: list[str], transfer_id: str) -> dict:
    """A transfer, its physical manifest, receipt stores and audit timeline."""

    record = db.get(Transfer, transfer_id)
    scope = set(facility_ids)
    if record is None or not (
        record.from_facility_id in scope or record.to_facility_id in scope
    ):
        raise ServiceError("TRANSFER_NOT_FOUND", "Transfer not found in this scope.")

    source = _facility(db, record.from_facility_id)
    destination = _facility(db, record.to_facility_id)
    component, donor_group = _component_and_group(db, record)
    recipient_group = (
        db.get(BloodGroup, record.recipient_group_id)
        if record.recipient_group_id is not None
        else None
    )
    manifest_ids = list(record.unit_ids or [])
    units_by_id = {
        unit.id: unit
        for unit in db.scalars(
            select(BloodUnit).where(BloodUnit.id.in_(manifest_ids))
        ).all()
    }
    units = [units_by_id[unit_id] for unit_id in manifest_ids if unit_id in units_by_id]
    units.sort(key=lambda unit: (as_utc(unit.expires_at) or DEMO_DATETIME, unit.din))
    stores = list(
        db.scalars(
            select(StorageLocation)
            .where(
                StorageLocation.facility_id == destination.id,
                StorageLocation.is_active.is_(True),
                StorageLocation.is_quarantine.is_(False),
            )
            .order_by(StorageLocation.name)
        ).all()
    )
    stores = [
        store
        for store in stores
        if (component.storage_temp_min_c is None or store.target_temp_min_c <= component.storage_temp_min_c)
        and (component.storage_temp_max_c is None or store.target_temp_max_c >= component.storage_temp_max_c)
        and (not component.requires_agitation or store.has_agitator)
    ]
    timeline = list(
        db.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "transfer", AuditLog.entity_id == record.id)
            .order_by(AuditLog.created_at)
        ).all()
    )
    return {
        "record": record,
        "source": source,
        "destination": destination,
        "component": component,
        "donor_group": donor_group,
        "recipient_group": recipient_group,
        "units": units,
        "stores": stores,
        "timeline": timeline,
        "is_source": record.from_facility_id in scope,
        "is_destination": record.to_facility_id in scope,
    }


# --------------------------------------------------------- governed writes ----

TRANSFER_AUDIT_FIELDS = (
    "status",
    "units",
    "unit_ids",
    "approved_by",
    "approved_at",
    "tracking_code",
    "dispatched_by",
    "dispatched_at",
    "in_transit_by",
    "in_transit_at",
    "received_by",
    "received_at",
    "receipt_disposition",
    "failed_reason",
    "cancelled_by",
    "cancelled_at",
)


def _transfer(db: Session, transfer_id: str) -> Transfer:
    record = db.get(Transfer, transfer_id)

    if record is None:
        raise ServiceError("TRANSFER_NOT_FOUND", "Transfer not found.")

    return record


def _expect(record: Transfer, *statuses: str) -> None:
    if record.status not in statuses:
        expected = ", ".join(statuses)
        raise ServiceError(
            "TRANSFER_STATE_INVALID",
            f"This action requires {expected}; the transfer is {record.status}.",
        )


def _facility(db: Session, facility_id: str) -> Facility:
    facility = db.get(Facility, facility_id)

    if facility is None:
        raise ServiceError("FACILITY_NOT_FOUND", "Transfer facility not found.")

    return facility


def _require_side(
    db: Session,
    actor: Actor,
    record: Transfer,
    *,
    side: str,
) -> Facility:
    facility_id = (
        record.from_facility_id if side == "source" else record.to_facility_id
    )
    facility = _facility(db, facility_id)

    role_scope_authority = (
        actor.role in {"RBC_COORDINATOR", "PROVINCIAL_ADMIN", "EMERGENCY_CONTROLLER"}
        and facility_id in set(actor.scope_facility_ids)
    )

    if side == "source" and actor.role == "EMERGENCY_CONTROLLER":
        incident = db.scalar(
            select(EmergencyIncident.id).where(
                EmergencyIncident.transfer_plan_id == record.plan_id,
                EmergencyIncident.status == "ACTIVE",
            )
        )
        if not incident:
            raise ServiceError(
                "EMERGENCY_AUTHORITY_REQUIRED",
                "Emergency control may act only on a transfer linked to an active incident.",
            )

    if (
        not actor.organization_id
        or (
            facility.organization_id != actor.organization_id
            and not role_scope_authority
        )
    ):
        # A 404-like domain refusal does not disclose whether another tenant's
        # shipment exists. The route maps this stable code to its not-found view.
        raise ServiceError(
            "TRANSFER_NOT_FOUND",
            "Transfer not found in this organization.",
        )

    if (
        not actor.organization_wide
        and actor.facility_id != facility_id
        and not role_scope_authority
    ):
        raise ServiceError(
            "TRANSFER_NOT_FOUND",
            "Transfer not found at this facility.",
        )

    return facility


def _manifest_units(db: Session, record: Transfer) -> list[BloodUnit]:
    manifest = list(record.unit_ids or [])

    if not manifest or len(manifest) != record.units or len(set(manifest)) != len(manifest):
        raise ServiceError(
            "MANIFEST_INVALID",
            "The physical-unit manifest does not match the recommended quantity.",
        )

    by_id = {
        unit.id: unit
        for unit in db.scalars(
            select(BloodUnit).where(BloodUnit.id.in_(manifest))
        ).all()
    }

    if len(by_id) != len(manifest):
        raise ServiceError(
            "MANIFEST_INVALID",
            "One or more physical units in the manifest no longer exist.",
        )

    return [by_id[unit_id] for unit_id in manifest]


def _component_and_group(db: Session, record: Transfer) -> tuple[Component, BloodGroup]:
    component = db.get(Component, record.component_id)
    group = db.get(BloodGroup, record.blood_group_id)

    if component is None or group is None:
        raise ServiceError(
            "REFERENCE_DATA_MISSING",
            "Component or blood-group reference data is missing.",
        )

    return component, group


def _other_claimed_ids(db: Session, record: Transfer) -> set[str]:
    claimed: set[str] = set()
    others = db.scalars(
        select(Transfer).where(
            Transfer.id != record.id,
            Transfer.status.in_(list(ACTIVE_TRANSFER_STATUSES)),
        )
    ).all()

    for other in others:
        claimed.update(other.unit_ids or [])

    return claimed


def _validate_for_approval(
    db: Session,
    record: Transfer,
    *,
    now: datetime,
) -> list[BloodUnit]:
    """Re-run the clinical constraints against today's physical stock.

    A recommendation can become stale between the solve and approval: a bag can
    be crossmatched, a test can be held, a cold-chain excursion can occur, or
    another approved shipment can claim the same reserve. Approval therefore
    trusts none of the optimizer's old inputs even though it preserves its unit
    selection.
    """

    units = _manifest_units(db, record)
    component, group = _component_and_group(db, record)
    source = _facility(db, record.from_facility_id)
    destination = _facility(db, record.to_facility_id)
    if source.organization_id != destination.organization_id:
        source_org = db.get(Organization, source.organization_id)
        destination_org = db.get(Organization, destination.organization_id)
        if (
            source_org is None
            or destination_org is None
            or not source_org.network_opt_in
            or not destination_org.network_opt_in
            or not source.shares_inventory
            or not destination.shares_inventory
        ):
            raise ServiceError(
                "NETWORK_SHARING_NOT_AUTHORIZED",
                "Both organizations and facilities must retain active network-sharing consent before approval.",
            )
    travel_minutes = int(record.est_travel_minutes or 0)
    max_minutes = float(component.max_transport_hours or 24.0) * 60.0

    if travel_minutes <= 0 or travel_minutes > max_minutes:
        raise ServiceError(
            "ROUTE_COLD_CHAIN_INVALID",
            "The journey no longer fits the component transport limit.",
        )

    # The compatibility path is revalidated as well; changing reference data
    # must not make an old plan executable by accident.
    from db.models import Compatibility

    compatible = db.scalar(
        select(func.count()).select_from(Compatibility).where(
            Compatibility.component_id == record.component_id,
            Compatibility.recipient_group_id == record.recipient_group_id,
            Compatibility.donor_group_id == record.blood_group_id,
            Compatibility.is_compatible.is_(True),
            Compatibility.requires_override.is_(False),
        )
    )
    if not compatible:
        raise ServiceError(
            "COMPATIBILITY_INVALID",
            "The recorded donor-to-recipient compatibility path is not permitted.",
        )

    cutoff = now + timedelta(
        minutes=travel_minutes,
        hours=float(config.get("expiry.handling_buffer_hours", 12)),
    )
    for unit in units:
        if (
            unit.facility_id != record.from_facility_id
            or unit.component_id != record.component_id
            or unit.blood_group_id != record.blood_group_id
        ):
            raise ServiceError(
                "MANIFEST_MISMATCH",
                f"Unit {unit.din} no longer matches the source series.",
            )
        if unit.status != "AVAILABLE":
            raise ServiceError(
                "UNIT_UNAVAILABLE",
                f"Unit {unit.din} is now {unit.status.lower()} and cannot be reserved.",
            )
        if unit.screening_status != "PASSED":
            raise ServiceError(
                "UNIT_NOT_RELEASED",
                f"Unit {unit.din} does not have a released screening result.",
            )
        if int(unit.cold_chain_breach_count or 0) > 0:
            raise ServiceError(
                "UNIT_COLD_CHAIN_HISTORY",
                f"Unit {unit.din} has a recorded cold-chain breach.",
            )
        if (as_utc(unit.expires_at) or now) <= cutoff:
            raise ServiceError(
                "UNIT_SHELF_LIFE_INSUFFICIENT",
                f"Unit {unit.din} cannot arrive with the required handling buffer.",
            )

    # Reserve is checked before FEFO so the most important facility-level veto
    # is reported first. A plan can be stale in more than one way after another
    # movement completes; a local clinical floor always outranks bag ordering.
    available = db.scalar(
        select(func.count()).select_from(BloodUnit).where(
            BloodUnit.facility_id == record.from_facility_id,
            BloodUnit.component_id == record.component_id,
            BloodUnit.blood_group_id == record.blood_group_id,
            BloodUnit.status == "AVAILABLE",
            BloodUnit.screening_status == "PASSED",
            BloodUnit.cold_chain_breach_count == 0,
            BloodUnit.expires_at > now,
        )
    ) or 0
    floor_factor = 1.0
    plan = db.get(TransferPlan, record.plan_id)
    if plan is not None and plan.plan_type == "EMERGENCY":
        incident_active = db.scalar(
            select(func.count()).select_from(EmergencyIncident).where(
                EmergencyIncident.transfer_plan_id == plan.id,
                EmergencyIncident.status == "ACTIVE",
            )
        )
        if incident_active:
            floor_factor = min(
                1.0,
                max(
                    0.0,
                    float((plan.parameters_json or {}).get("reserve_hold_factor", 1.0)),
                ),
            )
    floor = math.ceil(
        policy.reserve_floor(source, component.code, group.code) * floor_factor
    )
    if int(available) - record.units < floor:
        raise ServiceError(
            "RESERVE_FLOOR_BREACH",
            f"Approval would leave {int(available) - record.units} usable units, below the reserve floor of {floor}.",
        )

    # Preserve FEFO after excluding bags assigned to other still-live rows in
    # the plan. This catches a tampered manifest while allowing consolidated
    # routes to divide one source series between several recommendations.
    other_claimed = _other_claimed_ids(db, record)
    candidates = list(
        db.scalars(
            select(BloodUnit)
            .where(
                BloodUnit.facility_id == record.from_facility_id,
                BloodUnit.component_id == record.component_id,
                BloodUnit.blood_group_id == record.blood_group_id,
                BloodUnit.status == "AVAILABLE",
                BloodUnit.screening_status == "PASSED",
                BloodUnit.cold_chain_breach_count == 0,
                BloodUnit.expires_at > cutoff,
            )
            .order_by(BloodUnit.expires_at, BloodUnit.din)
        ).all()
    )
    candidates = [unit for unit in candidates if unit.id not in other_claimed]
    expected = {unit.id for unit in candidates[: record.units]}
    if {unit.id for unit in units} != expected:
        raise ServiceError(
            "FEFO_VIOLATION",
            "The manifest is no longer the first-expiry feasible selection; re-solve the plan.",
        )

    return units


def approve_transfer(
    db: Session,
    actor: Actor,
    transfer_id: str,
    *,
    now: datetime = DEMO_DATETIME,
) -> Transfer:
    require(actor, Permission.APPROVE_TRANSFER_OUT, "approve outbound transfers")
    record = _transfer(db, transfer_id)
    _require_side(db, actor, record, side="source")
    _expect(record, PENDING)
    now = as_utc(now) or DEMO_DATETIME
    units = _validate_for_approval(db, record, now=now)

    before = snapshot(record, TRANSFER_AUDIT_FIELDS)
    with audited(db, actor, "transfer.approve", "transfer", record.id) as entry:
        for unit in units:
            unit.status = "RESERVED"

        record.status = APPROVED
        record.approved_by = actor.display_name
        record.approved_at = now
        record.tracking_code = record.tracking_code or f"RH-{record.id[:8].upper()}"
        entry.on(record, before=before, after=snapshot(record, TRANSFER_AUDIT_FIELDS))
        entry.note(
            manifest_unit_ids=list(record.unit_ids or []),
            source_facility_id=record.from_facility_id,
            destination_facility_id=record.to_facility_id,
            inventory_transition="AVAILABLE→RESERVED",
        )

    clear_caches()
    return record


def reject_transfer(
    db: Session,
    actor: Actor,
    transfer_id: str,
    reason: str,
    note: str | None = None,
    *,
    now: datetime = DEMO_DATETIME,
) -> Transfer:
    require(actor, Permission.APPROVE_TRANSFER_OUT, "reject outbound transfers")
    if reason not in REJECTION_REASONS:
        raise ServiceError(
            "REJECTION_REASON_INVALID",
            "Select a structured rejection reason.",
            field="reason",
        )

    record = _transfer(db, transfer_id)
    _require_side(db, actor, record, side="source")
    _expect(record, PENDING)
    before = snapshot(record, TRANSFER_AUDIT_FIELDS)

    with audited(db, actor, "transfer.reject", "transfer", record.id) as entry:
        record.status = REJECTED
        record.rejection_reason = reason
        record.rejection_note = (note or "").strip() or None
        entry.on(record, before=before, after=snapshot(record, TRANSFER_AUDIT_FIELDS))
        entry.note(reason=reason, note=record.rejection_note, decision_time=now)

    clear_caches()
    return record


def modify_transfer_units(
    db: Session,
    actor: Actor,
    transfer_id: str,
    units: int,
    *,
    now: datetime = DEMO_DATETIME,
) -> Transfer:
    """Reduce quantity and retain the earliest-expiring bags from the manifest."""

    require(actor, Permission.APPROVE_TRANSFER_OUT, "modify outbound transfers")
    record = _transfer(db, transfer_id)
    _require_side(db, actor, record, side="source")
    _expect(record, PENDING)

    if units < 1 or units >= record.units:
        raise ServiceError(
            "QUANTITY_INVALID",
            f"Enter a quantity from 1 to {max(1, record.units - 1)}.",
            field="units",
        )

    manifest = _manifest_units(db, record)
    selected = sorted(
        manifest,
        key=lambda unit: (as_utc(unit.expires_at) or DEMO_DATETIME, unit.din),
    )[:units]
    before = snapshot(record, TRANSFER_AUDIT_FIELDS)
    old_units = record.units

    with audited(db, actor, "transfer.modify", "transfer", record.id) as entry:
        record.unit_ids = [unit.id for unit in selected]
        record.units = units
        if record.projected_units_saved is not None and old_units:
            record.projected_units_saved = float(record.projected_units_saved) * units / old_units
        if record.projected_shortage_averted is not None and old_units:
            record.projected_shortage_averted = (
                float(record.projected_shortage_averted) * units / old_units
            )
        record.modified_by = actor.display_name
        record.modified_at = as_utc(now) or DEMO_DATETIME
        entry.on(record, before=before, after=snapshot(record, TRANSFER_AUDIT_FIELDS))
        entry.note(
            released_from_recommendation=old_units - units,
            retained_unit_ids=list(record.unit_ids),
        )

    clear_caches()
    return record


def _temperature_in_range(value: float, component: Component) -> bool:
    minimum = component.storage_temp_min_c
    maximum = component.storage_temp_max_c
    return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)


def dispatch_transfer(
    db: Session,
    actor: Actor,
    transfer_id: str,
    *,
    custodian: str,
    courier_name: str,
    courier_phone: str | None,
    vehicle_ref: str | None,
    container_id: str,
    seal_number: str,
    departure_temp_c: float,
    now: datetime = DEMO_DATETIME,
) -> Transfer:
    require(actor, Permission.APPROVE_TRANSFER_OUT, "dispatch outbound transfers")
    record = _transfer(db, transfer_id)
    _require_side(db, actor, record, side="source")
    _expect(record, APPROVED)
    component, _ = _component_and_group(db, record)
    manifest = _manifest_units(db, record)

    required_values = {
        "custodian": custodian,
        "courier_name": courier_name,
        "container_id": container_id,
        "seal_number": seal_number,
    }
    missing = next((field for field, value in required_values.items() if not (value or "").strip()), None)
    if missing:
        raise ServiceError(
            "CUSTODY_REQUIRED",
            "Complete the custodian, courier, container and seal record.",
            field=missing,
        )
    if not _temperature_in_range(float(departure_temp_c), component):
        raise ServiceError(
            "DEPARTURE_TEMPERATURE_INVALID",
            f"Departure temperature must be within {component.storage_temp_min_c:g}–{component.storage_temp_max_c:g} °C.",
            field="departure_temp_c",
        )
    for unit in manifest:
        if unit.status != "RESERVED" or unit.facility_id != record.from_facility_id:
            raise ServiceError(
                "UNIT_NOT_RESERVED",
                f"Unit {unit.din} is not reserved at the source facility.",
            )

    event_time = max(as_utc(record.approved_at) or DEMO_DATETIME, as_utc(now) or DEMO_DATETIME)
    before = snapshot(record, TRANSFER_AUDIT_FIELDS)
    with audited(db, actor, "transfer.dispatch", "transfer", record.id) as entry:
        record.status = DISPATCHED
        record.dispatched_by = actor.display_name
        record.dispatched_at = event_time
        record.dispatch_custodian = custodian.strip()
        record.courier_name = courier_name.strip()
        record.courier_phone = (courier_phone or "").strip() or None
        record.vehicle_ref = (vehicle_ref or "").strip() or None
        record.container_id = container_id.strip()
        record.seal_number = seal_number.strip()
        record.departure_temp_c = float(departure_temp_c)
        entry.on(record, before=before, after=snapshot(record, TRANSFER_AUDIT_FIELDS))
        entry.note(
            custodian=record.dispatch_custodian,
            courier=record.courier_name,
            container_id=record.container_id,
            seal_number=record.seal_number,
            departure_temp_c=record.departure_temp_c,
            unit_count=len(manifest),
        )

    clear_caches()
    return record


def mark_in_transit(
    db: Session,
    actor: Actor,
    transfer_id: str,
    *,
    now: datetime = DEMO_DATETIME,
) -> Transfer:
    require(actor, Permission.APPROVE_TRANSFER_OUT, "release outbound transfers to transit")
    record = _transfer(db, transfer_id)
    _require_side(db, actor, record, side="source")
    _expect(record, DISPATCHED)
    manifest = _manifest_units(db, record)
    for unit in manifest:
        if unit.status != "RESERVED" or unit.facility_id != record.from_facility_id:
            raise ServiceError(
                "UNIT_NOT_RESERVED",
                f"Unit {unit.din} is not reserved at the source facility.",
            )

    event_time = max(as_utc(record.dispatched_at) or DEMO_DATETIME, as_utc(now) or DEMO_DATETIME)
    before = snapshot(record, TRANSFER_AUDIT_FIELDS)
    with audited(db, actor, "transfer.depart", "transfer", record.id) as entry:
        for unit in manifest:
            unit.status = "IN_TRANSIT"
            unit.storage_location_id = None
        record.status = IN_TRANSIT
        record.in_transit_by = actor.display_name
        record.in_transit_at = event_time
        entry.on(record, before=before, after=snapshot(record, TRANSFER_AUDIT_FIELDS))
        entry.note(
            inventory_transition="RESERVED→IN_TRANSIT",
            manifest_unit_ids=list(record.unit_ids or []),
        )

    clear_caches()
    return record


def _storage_location(
    db: Session,
    location_id: str,
    *,
    destination_id: str,
    component: Component,
    quarantine: bool,
    incoming: int,
) -> StorageLocation:
    location = db.get(StorageLocation, location_id)
    if (
        location is None
        or location.facility_id != destination_id
        or not location.is_active
        or bool(location.is_quarantine) != quarantine
    ):
        raise ServiceError(
            "STORAGE_LOCATION_INVALID",
            "Select an active destination storage location of the correct type.",
            field="storage_location_id",
        )
    if (
        component.storage_temp_min_c is not None
        and location.target_temp_min_c > component.storage_temp_min_c
    ) or (
        component.storage_temp_max_c is not None
        and location.target_temp_max_c < component.storage_temp_max_c
    ):
        raise ServiceError(
            "STORAGE_TEMPERATURE_INCOMPATIBLE",
            "The selected store does not maintain this component's temperature range.",
            field="storage_location_id",
        )
    if component.requires_agitation and not location.has_agitator:
        raise ServiceError(
            "AGITATION_REQUIRED",
            "This component must be placed in an agitator-equipped store.",
            field="storage_location_id",
        )
    occupied = db.scalar(
        select(func.count()).select_from(BloodUnit).where(
            BloodUnit.storage_location_id == location.id,
            BloodUnit.status.in_(("AVAILABLE", "RESERVED", "CROSSMATCHED", "QUARANTINE")),
        )
    ) or 0
    if location.capacity_units is not None and int(occupied) + incoming > location.capacity_units:
        raise ServiceError(
            "STORAGE_CAPACITY_EXCEEDED",
            f"The selected store has space for only {max(0, location.capacity_units - int(occupied))} more units.",
            field="storage_location_id",
        )
    return location


def _find_quarantine_location(
    db: Session,
    destination_id: str,
    component: Component,
    incoming: int,
) -> StorageLocation:
    criteria = [
        StorageLocation.facility_id == destination_id,
        StorageLocation.is_active.is_(True),
    ]
    if component.requires_agitation:
        # The configured platelet exception is status-segregated in the shared
        # agitator: a second incubator at every hospital is not realistic, while
        # stopping agitation would itself damage the unit. Red cells and frozen
        # products still require a physically designated quarantine store.
        criteria.append(StorageLocation.has_agitator.is_(True))
    else:
        criteria.append(StorageLocation.is_quarantine.is_(True))

    candidates = db.scalars(select(StorageLocation).where(*criteria)).all()
    for candidate in candidates:
        try:
            return _storage_location(
                db,
                candidate.id,
                destination_id=destination_id,
                component=component,
                quarantine=bool(candidate.is_quarantine),
                incoming=incoming,
            )
        except ServiceError:
            continue
    raise ServiceError(
        "QUARANTINE_STORAGE_UNAVAILABLE",
        "No compatible quarantine store has enough capacity for the exception units.",
    )


def receive_transfer(
    db: Session,
    actor: Actor,
    transfer_id: str,
    *,
    received_unit_ids: list[str],
    accepted_unit_ids: list[str],
    receiving_temp_c: float,
    seal_status: str,
    storage_location_id: str | None,
    discrepancy_note: str | None = None,
    now: datetime | None = None,
) -> Transfer:
    require(actor, Permission.ACCEPT_TRANSFER_IN, "receive inbound transfers")
    record = _transfer(db, transfer_id)
    _require_side(db, actor, record, side="destination")
    _expect(record, IN_TRANSIT)
    component, _ = _component_and_group(db, record)
    manifest = _manifest_units(db, record)
    manifest_ids = [unit.id for unit in manifest]
    received_ids = list(dict.fromkeys(received_unit_ids or []))
    accepted_ids = list(dict.fromkeys(accepted_unit_ids or []))

    if seal_status not in SEAL_STATUSES:
        raise ServiceError("SEAL_STATUS_INVALID", "Record the seal as intact, broken or missing.")
    if not set(received_ids).issubset(manifest_ids):
        raise ServiceError(
            "RECEIPT_UNIT_UNKNOWN",
            "The receipt includes a unit that is not on this manifest.",
        )
    if not set(accepted_ids).issubset(received_ids):
        raise ServiceError(
            "ACCEPTED_UNIT_NOT_RECEIVED",
            "An accepted unit must first be marked as physically received.",
        )
    for unit in manifest:
        if unit.status != "IN_TRANSIT":
            raise ServiceError(
                "UNIT_NOT_IN_TRANSIT",
                f"Unit {unit.din} is no longer recorded in transit.",
            )

    inferred_arrival = (as_utc(record.in_transit_at) or DEMO_DATETIME) + timedelta(
        minutes=max(1, int(record.est_travel_minutes or 1))
    )
    event_time = max(inferred_arrival, as_utc(now) or inferred_arrival)
    temperature_breach = not _temperature_in_range(float(receiving_temp_c), component)
    seal_breach = seal_status != "INTACT"
    expired_ids = {
        unit.id for unit in manifest if unit.id in received_ids and (as_utc(unit.expires_at) or event_time) <= event_time
    }
    global_exception = temperature_breach or seal_breach or bool(expired_ids)

    if global_exception:
        accepted_ids = []
        quarantined_ids = list(received_ids)
    else:
        quarantined_ids = [unit_id for unit_id in received_ids if unit_id not in set(accepted_ids)]
    missing_ids = [unit_id for unit_id in manifest_ids if unit_id not in set(received_ids)]

    accepted_store = None
    if accepted_ids:
        if not storage_location_id:
            raise ServiceError(
                "STORAGE_LOCATION_REQUIRED",
                "Select the destination store for accepted units.",
                field="storage_location_id",
            )
        accepted_store = _storage_location(
            db,
            storage_location_id,
            destination_id=record.to_facility_id,
            component=component,
            quarantine=False,
            incoming=len(accepted_ids),
        )
    quarantine_store = (
        _find_quarantine_location(db, record.to_facility_id, component, len(quarantined_ids))
        if quarantined_ids
        else None
    )

    by_id = {unit.id: unit for unit in manifest}
    before = snapshot(record, TRANSFER_AUDIT_FIELDS)
    with audited(db, actor, "transfer.receive", "transfer", record.id) as entry:
        for unit_id in accepted_ids:
            unit = by_id[unit_id]
            unit.facility_id = record.to_facility_id
            unit.storage_location_id = accepted_store.id if accepted_store else None
            unit.status = "AVAILABLE"
        for unit_id in quarantined_ids:
            unit = by_id[unit_id]
            unit.facility_id = record.to_facility_id
            unit.storage_location_id = quarantine_store.id if quarantine_store else None
            unit.status = "QUARANTINE"
            if temperature_breach:
                unit.cold_chain_breach_count = int(unit.cold_chain_breach_count or 0) + 1
        for unit_id in missing_ids:
            by_id[unit_id].status = "MISSING_IN_TRANSIT"

        record.received_by = actor.display_name
        record.received_at = event_time
        record.receiving_temp_c = float(receiving_temp_c)
        record.seal_status = seal_status
        record.received_unit_ids = received_ids
        record.accepted_unit_ids = accepted_ids
        record.quarantined_unit_ids = quarantined_ids
        record.missing_unit_ids = missing_ids
        record.discrepancy_note = (discrepancy_note or "").strip() or None
        record.discrepancy_json = {
            "manifest": len(manifest_ids),
            "received": len(received_ids),
            "accepted": len(accepted_ids),
            "quarantined": len(quarantined_ids),
            "missing": len(missing_ids),
            "temperature_breach": temperature_breach,
            "seal_breach": seal_breach,
            "expired_unit_ids": sorted(expired_ids),
        }
        if global_exception:
            record.status = FAILED_COLD_CHAIN
            reasons = []
            if temperature_breach:
                reasons.append("TEMPERATURE_OUT_OF_RANGE")
            if seal_breach:
                reasons.append("SEAL_INTEGRITY_FAILURE")
            if expired_ids:
                reasons.append("EXPIRED_IN_TRANSIT")
            record.failed_reason = "+".join(reasons)
            record.receipt_disposition = "QUARANTINED"
        else:
            record.status = RECEIVED
            record.receipt_disposition = (
                "COMPLETE" if len(accepted_ids) == len(manifest_ids) else "PARTIAL"
            )

        entry.on(record, before=before, after=snapshot(record, TRANSFER_AUDIT_FIELDS))
        entry.note(
            manifest_unit_ids=manifest_ids,
            accepted_unit_ids=accepted_ids,
            quarantined_unit_ids=quarantined_ids,
            missing_unit_ids=missing_ids,
            receiving_temp_c=record.receiving_temp_c,
            seal_status=seal_status,
            discrepancy=record.discrepancy_json,
        )

    clear_caches()
    return record


def cancel_transfer(
    db: Session,
    actor: Actor,
    transfer_id: str,
    reason: str,
    *,
    now: datetime = DEMO_DATETIME,
) -> Transfer:
    require(actor, Permission.APPROVE_TRANSFER_OUT, "cancel outbound transfers")
    record = _transfer(db, transfer_id)
    _require_side(db, actor, record, side="source")
    _expect(record, APPROVED, DISPATCHED)
    if not (reason or "").strip():
        raise ServiceError(
            "CANCELLATION_REASON_REQUIRED",
            "Explain why the approved movement is being cancelled.",
            field="reason",
        )
    manifest = _manifest_units(db, record)
    for unit in manifest:
        if unit.status != "RESERVED":
            raise ServiceError(
                "UNIT_NOT_RESERVED",
                f"Unit {unit.din} cannot be released from its current state.",
            )

    before = snapshot(record, TRANSFER_AUDIT_FIELDS)
    with audited(db, actor, "transfer.cancel", "transfer", record.id) as entry:
        for unit in manifest:
            unit.status = "AVAILABLE"
        record.status = CANCELLED
        record.cancelled_by = actor.display_name
        record.cancelled_at = as_utc(now) or DEMO_DATETIME
        record.cancellation_reason = reason.strip()
        entry.on(record, before=before, after=snapshot(record, TRANSFER_AUDIT_FIELDS))
        entry.note(
            reason=record.cancellation_reason,
            inventory_transition="RESERVED→AVAILABLE",
            manifest_unit_ids=list(record.unit_ids or []),
        )

    clear_caches()
    return record


@cached()
def rejection_breakdown() -> pd.DataFrame:
    """The feedback loop, visualised (spec §12.10 Operations tab)."""

    frame = read_sql(
        select(
            Transfer.rejection_reason,
            func.count().label("count"),
        )
        .where(Transfer.status == REJECTED)
        .group_by(Transfer.rejection_reason)
    )

    return frame
