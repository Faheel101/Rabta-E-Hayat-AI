"""Forecast history, quantiles, projected stock and model diagnostics."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
from sqlalchemy import select

from config.calendar import get_calendar_flags
from config.settings import DEMO_DATE
from db.models import (
    Forecast,
    ForecastMetric,
    ForecastRunSummary,
    MartDailyDemand,
    MartDaysOfCover,
    ShortageRisk,
)
from services.common import cached, read_sql


@cached()
def history(
    facility_id: str,
    component_id: int,
    blood_group_id: int,
    days: int = 120,
) -> pd.DataFrame:
    start = DEMO_DATE - timedelta(days=days)

    frame = read_sql(
        select(
            MartDailyDemand.demand_date,
            MartDailyDemand.units_requested,
            MartDailyDemand.units_issued,
            MartDailyDemand.units_unmet,
        )
        .where(
            MartDailyDemand.facility_id == facility_id,
            MartDailyDemand.component_id == component_id,
            MartDailyDemand.blood_group_id == blood_group_id,
            MartDailyDemand.demand_date >= start,
        )
        .order_by(MartDailyDemand.demand_date)
    )

    if frame.empty:
        return frame

    frame["demand_date"] = pd.to_datetime(frame["demand_date"])

    return frame


@cached()
def quantiles(
    facility_id: str,
    component_id: int,
    blood_group_id: int,
) -> pd.DataFrame:
    frame = read_sql(
        select(
            Forecast.target_date,
            Forecast.horizon_days,
            Forecast.p10,
            Forecast.p50,
            Forecast.p90,
            Forecast.model_version,
        )
        .where(
            Forecast.facility_id == facility_id,
            Forecast.component_id == component_id,
            Forecast.blood_group_id == blood_group_id,
        )
        .order_by(Forecast.target_date)
    )

    if frame.empty:
        return frame

    frame["target_date"] = pd.to_datetime(frame["target_date"])

    return frame


@cached()
def diagnostics(
    facility_id: str,
    component_id: int,
    blood_group_id: int,
    horizon_days: int = 7,
) -> dict:
    """Per-series backtest result, including the unflattering parts."""

    frame = read_sql(
        select(ForecastMetric).where(
            ForecastMetric.facility_id == facility_id,
            ForecastMetric.component_id == component_id,
            ForecastMetric.blood_group_id == blood_group_id,
            ForecastMetric.horizon_days == horizon_days,
        )
    )

    if frame.empty:
        return {}

    row = frame.iloc[0].to_dict()

    model_wape = row.get("wape")
    naive_wape = row.get("baseline_seasonal_naive_wape")

    row["skill_vs_naive"] = (
        round(1.0 - model_wape / naive_wape, 3)
        if model_wape is not None and naive_wape not in (None, 0)
        else None
    )

    return row


@cached()
def network_diagnostics() -> dict:
    frame = read_sql(select(ForecastRunSummary))

    return frame.iloc[0].to_dict() if not frame.empty else {}


@cached()
def projected_stock(
    facility_id: str,
    component_id: int,
    blood_group_id: int,
) -> pd.DataFrame:
    """Projected on-hand against the reserve floor across the risk horizon."""

    frame = read_sql(
        select(
            ShortageRisk.risk_date,
            ShortageRisk.horizon_days,
            ShortageRisk.on_hand_base,
            ShortageRisk.projected_available,
            ShortageRisk.required_p50,
            ShortageRisk.required_p90,
            ShortageRisk.reserve_floor,
            ShortageRisk.shortage_probability,
            ShortageRisk.risk_bucket,
        )
        .where(
            ShortageRisk.facility_id == facility_id,
            ShortageRisk.component_id == component_id,
            ShortageRisk.blood_group_id == blood_group_id,
        )
        .order_by(ShortageRisk.risk_date)
    )

    if frame.empty:
        return frame

    frame["risk_date"] = pd.to_datetime(frame["risk_date"])

    return frame


def first_breach(projection: pd.DataFrame) -> dict:
    """When the projection crosses the reserve floor, and when it hits zero."""

    if projection.empty:
        return {}

    breach = projection[
        projection["projected_available"] <= projection["reserve_floor"]
    ]
    stockout = projection[projection["projected_available"] <= 0]

    return {
        "reserve_breach_date": (
            breach.iloc[0]["risk_date"].date() if not breach.empty else None
        ),
        "stockout_date": (
            stockout.iloc[0]["risk_date"].date() if not stockout.empty else None
        ),
        "peak_probability": float(projection["shortage_probability"].max()),
        "worst_bucket": projection.loc[
            projection["shortage_probability"].idxmax(), "risk_bucket"
        ],
    }


@cached()
def series_for_facility(facility_id: str) -> pd.DataFrame:
    """Every measurable series at a facility, for the compare grid."""

    frame = read_sql(
        select(
            MartDaysOfCover.component_id,
            MartDaysOfCover.blood_group_id,
            MartDaysOfCover.units_available,
            MartDaysOfCover.days_of_cover,
            MartDaysOfCover.avg_daily_demand,
            MartDaysOfCover.risk_bucket,
            MartDaysOfCover.shortage_probability,
        ).where(MartDaysOfCover.facility_id == facility_id)
    )

    return frame


def calendar_bands(start, end) -> list[dict]:
    """Ramadan, Eid and Muharram windows overlapping a date range.

    Spec §12.5 shows these on the forecast chart, and they are the reason the
    forecast moves the way it does — an unlabelled 2.2x spike on Eid ul-Adha
    looks like a modelling error.
    """

    bands = []
    current = None

    day = pd.Timestamp(start).date()
    last = pd.Timestamp(end).date()

    labels = {
        "ramadan": ("fc.calendar_ramadan", "#1565c0"),
        "eid_fitr": ("fc.calendar_eid_fitr", "#2e7d32"),
        "eid_adha": ("fc.calendar_eid_adha", "#f57c00"),
        "muharram": ("fc.calendar_muharram", "#6a1b9a"),
    }

    while day <= last:
        flags = get_calendar_flags(day)
        active = next((name for name in labels if flags.get(name)), None)

        if active != (current["flag"] if current else None):
            if current:
                bands.append(current)
                current = None

            if active:
                key, colour = labels[active]
                current = {
                    "flag": active,
                    "label_key": key,
                    "colour": colour,
                    "start": day,
                    "end": day,
                }
        elif current:
            current["end"] = day

        day += timedelta(days=1)

    if current:
        bands.append(current)

    return bands
