"""Build the analytical marts (spec §4.3).

    python -m scripts.build_marts

Spec §12.13 sets a two-second page load on a 3G connection at a district
hospital, and §12.1 assumes an officer opens this once a day for four minutes.
Neither survives a page that scans 645,000 inventory snapshots and 265,000
demand events on every rerun, so the pages read pre-aggregated rows and nothing
else.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pandas as pd
from sqlalchemy import case, func, insert, select, tuple_

from config.settings import DEMO_DATE
from core import policy
from db.models import (
    BloodGroup,
    BloodUnit,
    Component,
    DemandEvent,
    Donation,
    ExpiryRescue,
    Facility,
    InventorySnapshot,
    MartDailyDemand,
    MartDaysOfCover,
    MartFacilityKpi,
    MartImpact,
    ShortageRisk,
    Transfer,
)
from db.session import SessionLocal, init_db

DEMO_DATETIME = datetime.combine(DEMO_DATE, time(8, 0, 0), tzinfo=timezone.utc)

DEMAND_WINDOW_DAYS = 28
KPI_WINDOW_DAYS = 30
FEED_STALE_HOURS = 36
FEED_OFFLINE_HOURS = 72

CHUNK = 5000


def bulk_insert(session, model, rows):
    for start in range(0, len(rows), CHUNK):
        session.execute(insert(model), rows[start : start + CHUNK])
        session.flush()

    return len(rows)


def build_daily_demand(session, *, series_keys: list[tuple] | None = None) -> int:
    """Gap-filled daily series. Zeros must be explicit."""

    statement = (
        select(
            DemandEvent.facility_id,
            DemandEvent.component_id,
            DemandEvent.blood_group_id,
            func.date(DemandEvent.requested_at).label("demand_date"),
            func.sum(DemandEvent.units_requested).label("units_requested"),
            func.sum(DemandEvent.units_issued).label("units_issued"),
        )
        .where(DemandEvent.outcome != "CANCELLED")
        .group_by(
            DemandEvent.facility_id,
            DemandEvent.component_id,
            DemandEvent.blood_group_id,
            func.date(DemandEvent.requested_at),
        )
    )

    if series_keys is not None:
        if not series_keys:
            return 0
        statement = statement.where(
            tuple_(
                DemandEvent.facility_id,
                DemandEvent.component_id,
                DemandEvent.blood_group_id,
            ).in_(series_keys)
        )

    frame = pd.read_sql(statement, session.connection())

    if frame.empty:
        return 0

    frame["demand_date"] = pd.to_datetime(frame["demand_date"]).dt.date

    # Only fill gaps within each series' own observed span; padding a series that
    # started late with zeros back to the start of history would invent history.
    rows = []

    for (facility_id, component_id, group_id), group in frame.groupby(
        ["facility_id", "component_id", "blood_group_id"], sort=False
    ):
        # Reindex only the measure columns: the key columns are strings and
        # cannot take a zero fill value.
        indexed = (
            group.set_index("demand_date")[["units_requested", "units_issued"]]
            .astype("int64")
            .sort_index()
        )

        full_range = pd.date_range(
            indexed.index.min(), indexed.index.max(), freq="D"
        ).date

        filled = indexed.reindex(full_range, fill_value=0)

        for demand_date, record in filled.iterrows():
            requested = int(record["units_requested"])
            issued = int(record["units_issued"])

            rows.append(
                {
                    "facility_id": facility_id,
                    "component_id": component_id,
                    "blood_group_id": group_id,
                    "demand_date": demand_date,
                    "units_requested": requested,
                    "units_issued": issued,
                    "units_unmet": max(0, requested - issued),
                }
            )

    return bulk_insert(session, MartDailyDemand, rows)


def build_days_of_cover(
    session, facilities, components, groups, *, generated_at: datetime
) -> int:
    facility_by_id = {facility.id: facility for facility in facilities}
    component_codes = {component.id: component.code for component in components}
    group_codes = {group.id: group.code for group in groups}

    units = pd.read_sql(
        select(
            BloodUnit.facility_id,
            BloodUnit.component_id,
            BloodUnit.blood_group_id,
            BloodUnit.status,
            BloodUnit.expires_at,
        ).where(
            BloodUnit.status.in_(["AVAILABLE", "RESERVED", "CROSSMATCHED"]),
            BloodUnit.screening_status == "PASSED",
        ),
        session.connection(),
    )

    units["expires_at"] = pd.to_datetime(units["expires_at"], utc=True)
    units = units[units["expires_at"] > DEMO_DATETIME]

    window_start = DEMO_DATE - timedelta(days=DEMAND_WINDOW_DAYS - 1)

    demand = pd.read_sql(
        select(
            MartDailyDemand.facility_id,
            MartDailyDemand.component_id,
            MartDailyDemand.blood_group_id,
            func.sum(MartDailyDemand.units_requested).label("requested"),
        )
        .where(
            MartDailyDemand.demand_date >= window_start,
            MartDailyDemand.demand_date <= DEMO_DATE,
        )
        .group_by(
            MartDailyDemand.facility_id,
            MartDailyDemand.component_id,
            MartDailyDemand.blood_group_id,
        ),
        session.connection(),
    )

    daily_demand = {
        (row.facility_id, row.component_id, row.blood_group_id): float(row.requested)
        / DEMAND_WINDOW_DAYS
        for row in demand.itertuples()
    }

    risk = pd.read_sql(
        select(
            ShortageRisk.facility_id,
            ShortageRisk.component_id,
            ShortageRisk.blood_group_id,
            ShortageRisk.shortage_probability,
            ShortageRisk.risk_bucket,
        ).where(ShortageRisk.horizon_days == 1),
        session.connection(),
    )

    risk_by_series = {
        (row.facility_id, row.component_id, row.blood_group_id): (
            float(row.shortage_probability),
            str(row.risk_bucket),
        )
        for row in risk.itertuples()
    }

    available = units[units["status"] == "AVAILABLE"]

    counts = available.groupby(
        ["facility_id", "component_id", "blood_group_id"]
    ).size()
    reserved = (
        units[units["status"] != "AVAILABLE"]
        .groupby(["facility_id", "component_id", "blood_group_id"])
        .size()
    )

    cutoff_72h = DEMO_DATETIME + timedelta(hours=72)
    cutoff_7d = DEMO_DATETIME + timedelta(days=7)

    expiring_72h = (
        available[available["expires_at"] <= cutoff_72h]
        .groupby(["facility_id", "component_id", "blood_group_id"])
        .size()
    )
    expiring_7d = (
        available[available["expires_at"] <= cutoff_7d]
        .groupby(["facility_id", "component_id", "blood_group_id"])
        .size()
    )

    rows = []

    for facility in facilities:
        for component in components:
            for group in groups:
                key = (facility.id, component.id, group.id)

                on_hand = int(counts.get(key, 0))
                demand_rate = float(daily_demand.get(key, 0.0))

                probability, bucket = risk_by_series.get(key, (0.0, "SAFE"))

                # A series with no demand has no meaningful days of cover; null
                # is honest, and the heatmap renders it grey rather than as
                # infinite safety.
                cover = (on_hand / demand_rate) if demand_rate > 0.01 else None

                if on_hand == 0 and demand_rate <= 0.01:
                    continue

                rows.append(
                    {
                        "facility_id": facility.id,
                        "component_id": component.id,
                        "blood_group_id": group.id,
                        "units_available": on_hand,
                        "units_reserved": int(reserved.get(key, 0)),
                        "units_expiring_72h": int(expiring_72h.get(key, 0)),
                        "units_expiring_7d": int(expiring_7d.get(key, 0)),
                        "avg_daily_demand": round(demand_rate, 3),
                        "days_of_cover": round(cover, 2) if cover is not None else None,
                        "reserve_floor": policy.reserve_floor(
                            facility,
                            component_codes[component.id],
                            group_codes[group.id],
                        ),
                        "shortage_probability": probability,
                        "risk_bucket": bucket,
                        "generated_at": generated_at,
                    }
                )

    return bulk_insert(session, MartDaysOfCover, rows)


def build_facility_kpi(
    session, facilities, components, groups, *, generated_at: datetime
) -> int:
    component_codes = {component.id: component.code for component in components}
    group_codes = {group.id: group.code for group in groups}

    cover = pd.read_sql(
        select(
            MartDaysOfCover.facility_id,
            MartDaysOfCover.component_id,
            MartDaysOfCover.blood_group_id,
            MartDaysOfCover.units_available,
            MartDaysOfCover.days_of_cover,
            MartDaysOfCover.risk_bucket,
        ),
        session.connection(),
    )

    rescue = pd.read_sql(
        select(
            ExpiryRescue.facility_id,
            ExpiryRescue.rescue_tier,
        ),
        session.connection(),
    )

    transfers = pd.read_sql(
        select(
            Transfer.from_facility_id,
            Transfer.to_facility_id,
            Transfer.status,
        ),
        session.connection(),
    )

    window_start = DEMO_DATE - timedelta(days=KPI_WINDOW_DAYS - 1)

    flows = pd.read_sql(
        select(
            InventorySnapshot.facility_id,
            func.sum(InventorySnapshot.units_collected).label("collected"),
            func.sum(InventorySnapshot.units_expired).label("expired"),
            func.sum(InventorySnapshot.units_discarded).label("discarded"),
        )
        .where(
            InventorySnapshot.snapshot_date >= window_start,
            # The snapshot loader closes completed days. Today's operational
            # truth comes from the transactional tables below.
            InventorySnapshot.snapshot_date < DEMO_DATE,
        )
        .group_by(InventorySnapshot.facility_id),
        session.connection(),
    ).set_index("facility_id")

    day_start = datetime.combine(DEMO_DATE, time.min, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    live_collected = dict(
        session.execute(
            select(Donation.facility_id, func.count(Donation.id))
            .where(
                Donation.collected_at >= day_start,
                Donation.collected_at < day_end,
            )
            .group_by(Donation.facility_id)
        ).all()
    )
    live_disposals = {
        facility_id: {
            "expired": int(expired or 0),
            "discarded": int(discarded or 0),
        }
        for facility_id, expired, discarded in session.execute(
            select(
                BloodUnit.facility_id,
                func.sum(
                    case((BloodUnit.discard_reason == "EXPIRY", 1), else_=0)
                ).label("expired"),
                func.sum(
                    case((BloodUnit.discard_reason == "EXPIRY", 0), else_=1)
                ).label("discarded"),
            )
            .where(
                BloodUnit.discarded_at >= day_start,
                BloodUnit.discarded_at < day_end,
            )
            .group_by(BloodUnit.facility_id)
        ).all()
    }

    demand = pd.read_sql(
        select(
            MartDailyDemand.facility_id,
            func.sum(MartDailyDemand.units_requested).label("requested"),
            func.sum(MartDailyDemand.units_issued).label("issued"),
        )
        .where(
            MartDailyDemand.demand_date >= window_start,
            MartDailyDemand.demand_date <= DEMO_DATE,
        )
        .group_by(MartDailyDemand.facility_id),
        session.connection(),
    ).set_index("facility_id")

    sync = pd.read_sql(
        select(
            BloodUnit.facility_id,
            func.max(BloodUnit.last_synced_at).label("last_synced_at"),
        ).group_by(BloodUnit.facility_id),
        session.connection(),
    ).set_index("facility_id")

    rows = []

    for facility in facilities:
        facility_cover = cover[cover["facility_id"] == facility.id]
        measurable = facility_cover[facility_cover["days_of_cover"].notna()]

        if not measurable.empty:
            worst = measurable.loc[measurable["days_of_cover"].idxmin()]
            min_cover = float(worst["days_of_cover"])
            worst_component = component_codes.get(int(worst["component_id"]))
            worst_group = group_codes.get(int(worst["blood_group_id"]))
        else:
            min_cover = None
            worst_component = None
            worst_group = None

        facility_rescue = rescue[rescue["facility_id"] == facility.id]

        current_disposals = live_disposals.get(facility.id, {})
        collected = float(flows["collected"].get(facility.id, 0) or 0) + float(
            live_collected.get(facility.id, 0) or 0
        )
        expired = float(flows["expired"].get(facility.id, 0) or 0) + float(
            current_disposals.get("expired", 0)
        )
        discarded = float(flows["discarded"].get(facility.id, 0) or 0) + float(
            current_disposals.get("discarded", 0)
        )

        requested = float(demand["requested"].get(facility.id, 0) or 0)
        issued = float(demand["issued"].get(facility.id, 0) or 0)

        last_synced = sync["last_synced_at"].get(facility.id)
        feed_status, feed_age = classify_feed(last_synced)

        # Positive means the facility holds more than a balanced position.
        critical = int((facility_cover["risk_bucket"] == "CRITICAL").sum())
        warning = int((facility_cover["risk_bucket"] == "WARNING").sum())
        total_series = max(1, len(facility_cover))

        rows.append(
            {
                "facility_id": facility.id,
                "facility_code": facility.code,
                "name_en": facility.name_en,
                "name_ur": facility.name_ur,
                "facility_type": facility.facility_type,
                "district": facility.district,
                "division": facility.division,
                "latitude": float(facility.latitude),
                "longitude": float(facility.longitude),
                "parent_rbc_id": facility.parent_rbc_id,
                "units_available": int(facility_cover["units_available"].sum()),
                "min_days_of_cover": min_cover,
                "worst_component_code": worst_component,
                "worst_group_code": worst_group,
                "critical_series": critical,
                "warning_series": warning,
                "units_at_risk": int(
                    facility_rescue["rescue_tier"].isin(["ACT_NOW", "WATCH"]).sum()
                ),
                "units_unrescuable": int(
                    (facility_rescue["rescue_tier"] == "UNRESCUABLE").sum()
                ),
                "wastage_pct_30d": (
                    round(100.0 * (expired + discarded) / collected, 2)
                    if collected > 0
                    else None
                ),
                "fill_rate_30d": (
                    round(issued / requested, 4) if requested > 0 else None
                ),
                "transfers_in_pending": int(
                    (
                        (transfers["to_facility_id"] == facility.id)
                        & (transfers["status"] == "RECOMMENDED")
                    ).sum()
                ),
                "transfers_out_pending": int(
                    (
                        (transfers["from_facility_id"] == facility.id)
                        & (transfers["status"] == "RECOMMENDED")
                    ).sum()
                ),
                "integration_mode": facility.integration_mode,
                "last_synced_at": last_synced,
                "feed_status": feed_status,
                "feed_age_hours": feed_age,
                "balance_index": round(
                    1.0 - (critical + 0.5 * warning) / total_series, 3
                ),
                "generated_at": generated_at,
            }
        )

    return bulk_insert(session, MartFacilityKpi, rows)


def classify_feed(last_synced_at):
    """Spec §5.8: never silently drop a facility with a stale feed."""

    if last_synced_at is None:
        return "OFFLINE", None

    stamp = pd.Timestamp(last_synced_at)

    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")

    age_hours = (pd.Timestamp(DEMO_DATETIME) - stamp).total_seconds() / 3600.0

    if age_hours >= FEED_OFFLINE_HOURS:
        status = "OFFLINE"
    elif age_hours >= FEED_STALE_HOURS:
        status = "STALE"
    else:
        status = "HEALTHY"

    return status, round(age_hours, 1)


def build_impact(session) -> int:
    flows = pd.read_sql(
        select(
            InventorySnapshot.snapshot_date,
            func.sum(InventorySnapshot.units_collected).label("collected"),
            func.sum(InventorySnapshot.units_issued).label("issued"),
            func.sum(InventorySnapshot.units_expired).label("expired"),
            func.sum(InventorySnapshot.units_discarded).label("discarded"),
        )
        .where(InventorySnapshot.snapshot_date < DEMO_DATE)
        .group_by(InventorySnapshot.snapshot_date),
        session.connection(),
    )

    day_start = datetime.combine(DEMO_DATE, time.min, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    current_collected = int(
        session.scalar(
            select(func.count(Donation.id)).where(
                Donation.collected_at >= day_start,
                Donation.collected_at < day_end,
            )
        )
        or 0
    )
    current_expired = int(
        session.scalar(
            select(func.count(BloodUnit.id)).where(
                BloodUnit.discarded_at >= day_start,
                BloodUnit.discarded_at < day_end,
                BloodUnit.discard_reason == "EXPIRY",
            )
        )
        or 0
    )
    current_disposed_total = int(
        session.scalar(
            select(func.count(BloodUnit.id)).where(
                BloodUnit.discarded_at >= day_start,
                BloodUnit.discarded_at < day_end,
            )
        )
        or 0
    )
    current_discarded = max(0, current_disposed_total - current_expired)
    flows = pd.concat(
        [
            flows,
            pd.DataFrame(
                [
                    {
                        "snapshot_date": DEMO_DATE,
                        "collected": current_collected,
                        # Canonical demand below owns issued counts.
                        "issued": 0,
                        "expired": current_expired,
                        "discarded": current_discarded,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    flows["snapshot_date"] = pd.to_datetime(flows["snapshot_date"]).dt.date

    demand = pd.read_sql(
        select(
            MartDailyDemand.demand_date,
            func.sum(MartDailyDemand.units_requested).label("requested"),
            func.sum(MartDailyDemand.units_issued).label("issued"),
            func.sum(MartDailyDemand.units_unmet).label("unmet"),
        ).group_by(MartDailyDemand.demand_date),
        session.connection(),
    )

    demand["demand_date"] = pd.to_datetime(demand["demand_date"]).dt.date
    demand = demand.set_index("demand_date")

    rows = []

    for row in flows.itertuples():
        collected = int(row.collected or 0)
        expired = int(row.expired or 0)
        discarded = int(row.discarded or 0)

        requested = int(demand["requested"].get(row.snapshot_date, 0) or 0)
        unmet = int(demand["unmet"].get(row.snapshot_date, 0) or 0)

        rows.append(
            {
                "impact_date": row.snapshot_date,
                "units_collected": collected,
                "units_issued": int(
                    demand["issued"].get(row.snapshot_date, 0) or 0
                ),
                "units_expired": expired,
                "units_discarded": discarded,
                "units_requested": requested,
                "units_unmet": unmet,
                "wastage_pct": (
                    round(100.0 * (expired + discarded) / collected, 3)
                    if collected > 0
                    else None
                ),
                "fill_rate": (
                    round((requested - unmet) / requested, 4) if requested > 0 else None
                ),
            }
        )

    return bulk_insert(session, MartImpact, rows)


def rebuild(
    session,
    *,
    generated_at: datetime | None = None,
    full_demand: bool = True,
    clinical_series_keys: list[tuple] | None = None,
) -> dict:
    """Replace the derived marts without committing the caller's transaction."""

    generated_at = generated_at or datetime.now(timezone.utc)
    facilities = session.scalars(
        select(Facility).where(Facility.is_active.is_(True)).order_by(Facility.code)
    ).all()
    components = session.scalars(select(Component).order_by(Component.id)).all()
    groups = session.scalars(select(BloodGroup).order_by(BloodGroup.id)).all()

    for model in (MartDaysOfCover, MartFacilityKpi, MartImpact):
        session.query(model).delete(synchronize_session=False)

    selected_series = None
    if full_demand:
        session.query(MartDailyDemand).delete(synchronize_session=False)
    else:
        selected_series = list(clinical_series_keys or [])
        if selected_series:
            session.query(MartDailyDemand).filter(
                tuple_(
                    MartDailyDemand.facility_id,
                    MartDailyDemand.component_id,
                    MartDailyDemand.blood_group_id,
                ).in_(selected_series)
            ).delete(synchronize_session=False)
    session.flush()

    demand_rows = build_daily_demand(session, series_keys=selected_series)
    cover_rows = build_days_of_cover(
        session,
        facilities,
        components,
        groups,
        generated_at=generated_at,
    )
    kpi_rows = build_facility_kpi(
        session,
        facilities,
        components,
        groups,
        generated_at=generated_at,
    )
    impact_rows = build_impact(session)
    session.flush()

    stale = int(
        session.scalar(
            select(func.count())
            .select_from(MartFacilityKpi)
            .where(MartFacilityKpi.feed_status != "HEALTHY")
        )
        or 0
    )
    return {
        "demand_rows": demand_rows,
        "demand_refresh_mode": "FULL" if full_demand else "CLINICAL_SERIES",
        "cover_rows": cover_rows,
        "facility_kpi_rows": kpi_rows,
        "impact_rows": impact_rows,
        "facilities": len(facilities),
        "healthy_feeds": max(0, len(facilities) - stale),
    }


def main():
    init_db()
    session = SessionLocal()

    try:
        print("Refreshing analytical marts...")
        result = rebuild(session)
        session.commit()
        print("Marts complete.")
        for key, value in result.items():
            print(f"  {key}: {value:,}" if isinstance(value, int) else f"  {key}: {value}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
