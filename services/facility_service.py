"""Facility, network position and data-freshness reads."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
from sqlalchemy import func, select

from db.models import (
    BloodGroup,
    Component,
    ExpiryRescue,
    MartDaysOfCover,
    MartFacilityKpi,
    ShortageRisk,
    Transfer,
)
from services.common import DEMO_DATETIME, cached, read_sql


@cached()
def facilities() -> pd.DataFrame:
    """Every active facility with its current KPI row, ready for map and lists."""

    frame = read_sql(select(MartFacilityKpi))

    if frame.empty:
        return frame

    frame["last_synced_at"] = pd.to_datetime(frame["last_synced_at"], utc=True)

    return frame.sort_values("name_en").reset_index(drop=True)


@cached()
def components() -> pd.DataFrame:
    return read_sql(
        select(
            Component.id.label("component_id"),
            Component.code,
            Component.name_en,
            Component.shelf_life_days,
            Component.storage_temp_min_c,
            Component.storage_temp_max_c,
            Component.requires_agitation,
            Component.max_transport_hours,
            Component.criticality_weight,
        ).order_by(Component.id)
    )


@cached()
def blood_groups() -> pd.DataFrame:
    return read_sql(
        select(
            BloodGroup.id.label("blood_group_id"),
            BloodGroup.code,
            BloodGroup.abo,
            BloodGroup.rh,
            BloodGroup.population_pct_pk,
        ).order_by(BloodGroup.id)
    )


@cached()
def days_of_cover(facility_ids: tuple[str, ...]) -> pd.DataFrame:
    """Current stock position per series for the given facilities."""

    if not facility_ids:
        return pd.DataFrame()

    frame = read_sql(
        select(MartDaysOfCover).where(
            MartDaysOfCover.facility_id.in_(list(facility_ids))
        )
    )

    if frame.empty:
        return frame

    component_codes = components().set_index("component_id")["code"].to_dict()
    group_codes = blood_groups().set_index("blood_group_id")["code"].to_dict()

    frame["component_code"] = frame["component_id"].map(component_codes)
    frame["group_code"] = frame["blood_group_id"].map(group_codes)

    return frame


@cached()
def network_summary(facility_ids: tuple[str, ...]) -> dict:
    """The five Command Centre tiles (spec §12.4)."""

    if not facility_ids:
        return {}

    ids = list(facility_ids)

    kpi = facilities()
    kpi = kpi[kpi["facility_id"].isin(ids)]

    cover = days_of_cover(facility_ids)

    risk = read_sql(
        select(
            ShortageRisk.facility_id,
            ShortageRisk.component_id,
            ShortageRisk.blood_group_id,
            ShortageRisk.risk_bucket,
            ShortageRisk.shortage_probability,
        ).where(ShortageRisk.facility_id.in_(ids))
    )

    # A series counts once, at its worst point in the horizon.
    if not risk.empty:
        worst = (
            risk.sort_values("shortage_probability", ascending=False)
            .groupby(["facility_id", "component_id", "blood_group_id"], as_index=False)
            .first()
        )
        alerting = worst[worst["risk_bucket"].isin(["WARNING", "CRITICAL"])]
        critical = worst[worst["risk_bucket"] == "CRITICAL"]
        healthy_share = (
            1.0 - len(alerting) / len(worst) if len(worst) else 1.0
        )
    else:
        alerting = critical = pd.DataFrame()
        healthy_share = 1.0

    rescue = read_sql(
        select(
            ExpiryRescue.facility_id,
            ExpiryRescue.rescue_tier,
            ExpiryRescue.days_left,
        ).where(ExpiryRescue.facility_id.in_(ids))
    )

    expiring_72h = 0
    expiring_72h_critical = 0

    if not rescue.empty:
        soon = rescue[rescue["days_left"] <= 3.0]
        expiring_72h = int(len(soon))
        expiring_72h_critical = int((soon["rescue_tier"] == "ACT_NOW").sum())

    pending = read_sql(
        select(func.count())
        .select_from(Transfer)
        .where(
            Transfer.status == "RECOMMENDED",
            Transfer.from_facility_id.in_(ids) | Transfer.to_facility_id.in_(ids),
        )
    )
    pending_transfers = int(pending.iloc[0, 0]) if not pending.empty else 0

    approved = read_sql(
        select(func.coalesce(func.sum(Transfer.units), 0)).where(
            Transfer.status.in_(["APPROVED", "DISPATCHED", "IN_TRANSIT", "RECEIVED"]),
            Transfer.from_facility_id.in_(ids),
        )
    )
    rescued_units = int(approved.iloc[0, 0]) if not approved.empty else 0

    return {
        "network_health_pct": round(100.0 * healthy_share, 1),
        "shortage_alerts": int(len(alerting)),
        "shortage_alerts_critical": int(len(critical)),
        "expiring_72h": expiring_72h,
        "expiring_72h_critical": expiring_72h_critical,
        "pending_transfers": pending_transfers,
        "units_rescued_mtd": rescued_units,
        "units_available": int(kpi["units_available"].sum()) if not kpi.empty else 0,
        "facilities": int(len(kpi)),
        "feeds_healthy": int((kpi["feed_status"] == "HEALTHY").sum())
        if not kpi.empty
        else 0,
        "feeds_total": int(len(kpi)),
    }


@cached()
def data_freshness() -> dict:
    """Spec §12.3: the freshness footer is always visible, because trust in this
    system rests on the user knowing exactly how old the data is."""

    kpi = facilities()

    if kpi.empty:
        return {
            "as_of": DEMO_DATETIME,
            "healthy": 0,
            "total": 0,
            "stale": [],
        }

    stale = kpi[kpi["feed_status"] != "HEALTHY"]

    return {
        "as_of": DEMO_DATETIME,
        "healthy": int((kpi["feed_status"] == "HEALTHY").sum()),
        "total": int(len(kpi)),
        "stale": stale[["name_en", "feed_status", "feed_age_hours"]].to_dict("records"),
    }


def facility_options(facility_ids: tuple[str, ...]) -> list[tuple[str, str]]:
    frame = facilities()

    if frame.empty:
        return []

    if facility_ids:
        frame = frame[frame["facility_id"].isin(list(facility_ids))]

    return [
        (row.facility_id, row.name_en)
        for row in frame.sort_values("name_en").itertuples()
    ]


def facility_row(facility_id: str) -> dict | None:
    frame = facilities()

    if frame.empty:
        return None

    match = frame[frame["facility_id"] == facility_id]

    return match.iloc[0].to_dict() if not match.empty else None


def default_facility_id() -> str | None:
    """A tertiary hospital makes the most legible landing state."""

    frame = facilities()

    if frame.empty:
        return None

    preferred = frame[frame["facility_code"] == "JINNAH_LAHORE"]

    if not preferred.empty:
        return str(preferred.iloc[0]["facility_id"])

    tertiary = frame[frame["facility_type"] == "TERTIARY_HOSPITAL"]
    pool = tertiary if not tertiary.empty else frame

    return str(pool.iloc[0]["facility_id"])
