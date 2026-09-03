"""Expiry rescue reads (spec §7, §12.6)."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
from sqlalchemy import select

from core import config
from db.models import BloodUnit, Component, ExpiryRescue, Facility, MartFacilityKpi
from services.common import DEMO_DATETIME, cached, read_sql, tier_sort_key

COST_PER_UNIT = float(config.get("impact.cost_per_unit_pkr", 15000))


@cached()
def rescue_rows(facility_ids: tuple[str, ...]) -> pd.DataFrame:
    """Scored units with the destination and reason attached, ready to render."""

    if not facility_ids:
        return pd.DataFrame()

    ids = list(facility_ids)

    frame = read_sql(
        select(
            ExpiryRescue.id,
            ExpiryRescue.blood_unit_id,
            ExpiryRescue.facility_id,
            ExpiryRescue.component_id,
            ExpiryRescue.blood_group_id,
            ExpiryRescue.expires_at,
            ExpiryRescue.days_left,
            ExpiryRescue.waste_probability,
            ExpiryRescue.rescue_tier,
            ExpiryRescue.transferable,
            ExpiryRescue.best_recipient_facility_id,
            ExpiryRescue.best_travel_minutes,
            ExpiryRescue.dispatch_deadline_at,
            ExpiryRescue.hours_to_deadline,
            ExpiryRescue.best_need_score,
            ExpiryRescue.rescue_value,
            ExpiryRescue.reason_en,
            BloodUnit.din,
            Component.code.label("component_code"),
            Component.max_transport_hours,
            Component.requires_agitation,
            Component.storage_temp_min_c,
            Component.storage_temp_max_c,
        )
        .join(BloodUnit, BloodUnit.id == ExpiryRescue.blood_unit_id)
        .join(Component, Component.id == ExpiryRescue.component_id)
        .where(ExpiryRescue.facility_id.in_(ids))
    )

    if frame.empty:
        return frame

    from services.facility_service import blood_groups, facilities

    group_codes = blood_groups().set_index("blood_group_id")["code"].to_dict()
    names = facilities().set_index("facility_id")["name_en"].to_dict()

    frame["group_code"] = frame["blood_group_id"].map(group_codes)
    frame["facility_name"] = frame["facility_id"].map(names)
    frame["destination_name"] = frame["best_recipient_facility_id"].map(names)

    frame["expires_at"] = pd.to_datetime(frame["expires_at"], utc=True)
    frame["dispatch_deadline_at"] = pd.to_datetime(
        frame["dispatch_deadline_at"], utc=True
    )
    frame["hours_left"] = frame["days_left"] * 24.0
    frame["tier_order"] = frame["rescue_tier"].map(tier_sort_key)

    return frame


@cached()
def summary(facility_ids: tuple[str, ...]) -> dict:
    """The four Expiry Rescue tiles."""

    frame = rescue_rows(facility_ids)

    if frame.empty:
        return {
            "at_risk": 0,
            "rescuable": 0,
            "unrescuable": 0,
            "not_transferable": 0,
            "rescued_mtd": 0,
            "value_at_risk": 0.0,
            "value_rescuable": 0.0,
            "value_rescued": 0.0,
        }

    within_7d = frame[frame["days_left"] <= 7.0]

    at_risk = within_7d[
        within_7d["rescue_tier"].isin(["ACT_NOW", "WATCH", "UNRESCUABLE"])
    ]
    rescuable = within_7d[within_7d["rescue_tier"].isin(["ACT_NOW", "WATCH"])]
    unrescuable = within_7d[within_7d["rescue_tier"] == "UNRESCUABLE"]

    from services.transfer_service import rescued_units_mtd

    rescued = rescued_units_mtd(facility_ids)

    return {
        "at_risk": int(len(at_risk)),
        "rescuable": int(len(rescuable)),
        "unrescuable": int(len(unrescuable)),
        "not_transferable": int(
            (within_7d["rescue_tier"] == "NOT_TRANSFERABLE").sum()
        ),
        "rescued_mtd": rescued,
        "value_at_risk": len(at_risk) * COST_PER_UNIT,
        "value_rescuable": len(rescuable) * COST_PER_UNIT,
        "value_rescued": rescued * COST_PER_UNIT,
    }


@cached()
def timeline(facility_ids: tuple[str, ...], days: int = 14) -> pd.DataFrame:
    """Units expiring per day, split by whether they are projected to be used."""

    frame = rescue_rows(facility_ids)

    if frame.empty:
        return pd.DataFrame()

    horizon = frame[frame["days_left"] <= days].copy()

    if horizon.empty:
        return pd.DataFrame()

    horizon["expiry_date"] = horizon["expires_at"].dt.date
    horizon["at_risk"] = horizon["rescue_tier"].isin(
        ["ACT_NOW", "WATCH", "UNRESCUABLE"]
    )

    grouped = (
        horizon.groupby("expiry_date")
        .agg(
            at_risk=("at_risk", "sum"),
            total=("at_risk", "count"),
        )
        .reset_index()
    )

    grouped["will_be_used"] = grouped["total"] - grouped["at_risk"]

    return grouped


@cached()
def prevention_suggestions(facility_ids: tuple[str, ...]) -> list[dict]:
    """Structural fixes, derived from repeated unrescuable holdings (spec §7.3).

    This is the line that turns the tool from reactive to structural: a facility
    that keeps appearing here does not have bad luck, it has the wrong standing
    allocation.
    """

    frame = rescue_rows(facility_ids)

    if frame.empty:
        return []

    unrescuable = frame[frame["rescue_tier"] == "UNRESCUABLE"]

    if unrescuable.empty:
        return []

    grouped = (
        unrescuable.groupby(
            ["facility_id", "facility_name", "component_code", "group_code"]
        )
        .size()
        .reset_index(name="units")
        .sort_values("units", ascending=False)
    )

    return [
        {
            "facility_id": row.facility_id,
            "facility_name": row.facility_name,
            "component_code": row.component_code,
            "group_code": row.group_code,
            "units": int(row.units),
        }
        for row in grouped.itertuples()
        if row.units >= 3
    ]
