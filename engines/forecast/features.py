"""Feature building and demand-regime classification (spec §6.2, §6.3)."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import func, select

from config.calendar import CALENDAR_FEATURES, get_calendar_flags
from db.models import BloodGroup, Component, DemandEvent, Facility

KEY_COLS = ["facility_id", "component_id", "blood_group_id"]

LAGS = [1, 2, 3, 7, 14, 21, 28]
ROLLING_WINDOWS = [7, 14, 28, 56]

SERIES_FEATURES = [
    "facility_type_code",
    "component_id",
    "blood_group_id",
    "population_pct",
    "rh_negative",
    "bed_count",
    "has_trauma_centre",
    "has_oncology",
    "has_thalassaemia_centre",
    "has_obgyn",
    "has_cardiac_surgery",
]

TIME_FEATURES = [
    "dayofweek",
    "day",
    "month",
    "weekofyear",
    "is_weekend",
]

LAG_FEATURES = (
    [f"lag_{lag}" for lag in LAGS]
    + [f"roll_mean_{window}" for window in ROLLING_WINDOWS]
    + [f"roll_std_{window}" for window in (7, 28)]
    + ["roll_max_28", "zero_frac_28"]
)

FEATURES = SERIES_FEATURES + TIME_FEATURES + CALENDAR_FEATURES + LAG_FEATURES

FACILITY_TYPE_MAP = {
    "RBC": 0,
    "TERTIARY_HOSPITAL": 1,
    "SPECIALIST_CENTRE": 2,
    "DHQ": 3,
    "THQ": 4,
}


def load_series(session, history_start: date, history_end: date):
    facility_df = pd.read_sql(
        select(
            Facility.id,
            Facility.facility_type,
            Facility.bed_count,
            Facility.has_trauma_centre,
            Facility.has_oncology,
            Facility.has_thalassaemia_centre,
            Facility.has_obgyn,
            Facility.has_cardiac_surgery,
        ),
        session.bind,
    ).rename(columns={"id": "facility_id"})

    component_df = pd.read_sql(select(Component.id), session.bind).rename(
        columns={"id": "component_id"}
    )

    group_df = pd.read_sql(
        select(BloodGroup.id, BloodGroup.population_pct_pk, BloodGroup.rh),
        session.bind,
    ).rename(columns={"id": "blood_group_id"})

    stmt = (
        select(
            DemandEvent.facility_id,
            DemandEvent.component_id,
            DemandEvent.blood_group_id,
            func.date(DemandEvent.requested_at).label("demand_date"),
            func.sum(DemandEvent.units_requested).label("y"),
        )
        .group_by(
            DemandEvent.facility_id,
            DemandEvent.component_id,
            DemandEvent.blood_group_id,
            func.date(DemandEvent.requested_at),
        )
    )

    agg = pd.read_sql(stmt, session.bind)
    agg["demand_date"] = pd.to_datetime(agg["demand_date"]).dt.date
    agg["y"] = agg["y"].astype(float)

    agg = agg[
        (agg["demand_date"] >= history_start) & (agg["demand_date"] <= history_end)
    ]

    return facility_df, component_df, group_df, agg


def build_panel(facility_df, component_df, group_df, agg_df, start: date, end: date):
    """Gap-filled daily panel. Zeros must be explicit or the model learns nothing
    about the days a rare group was not requested."""

    dates = pd.date_range(start, end, freq="D").date

    base = pd.MultiIndex.from_product(
        [
            facility_df["facility_id"].tolist(),
            component_df["component_id"].tolist(),
            group_df["blood_group_id"].tolist(),
            dates,
        ],
        names=KEY_COLS + ["demand_date"],
    ).to_frame(index=False)

    df = base.merge(agg_df, on=KEY_COLS + ["demand_date"], how="left")
    df["y"] = df["y"].fillna(0.0).astype(float)

    df = df.merge(facility_df, on="facility_id", how="left")
    df = df.merge(group_df, on="blood_group_id", how="left")

    df["facility_type_code"] = (
        df["facility_type"].map(FACILITY_TYPE_MAP).fillna(5).astype(int)
    )
    df["population_pct"] = df["population_pct_pk"].fillna(0.0).astype(float)
    df["rh_negative"] = (df["rh"] == "-").astype(int)
    df["bed_count"] = df["bed_count"].fillna(0).astype(float)

    for flag in (
        "has_trauma_centre",
        "has_oncology",
        "has_thalassaemia_centre",
        "has_obgyn",
        "has_cardiac_surgery",
    ):
        df[flag] = df[flag].fillna(False).astype(int)

    calendar_df = pd.DataFrame(
        [{"demand_date": day, **get_calendar_flags(day)} for day in dates]
    )
    df = df.merge(calendar_df, on="demand_date", how="left")

    df["dayofweek"] = pd.to_datetime(df["demand_date"]).dt.dayofweek
    df["day"] = pd.to_datetime(df["demand_date"]).dt.day
    df["month"] = pd.to_datetime(df["demand_date"]).dt.month
    df["weekofyear"] = (
        pd.to_datetime(df["demand_date"]).dt.isocalendar().week.astype(int)
    )
    df["is_weekend"] = (df["dayofweek"] >= 5).astype(int)

    return df.sort_values(KEY_COLS + ["demand_date"]).reset_index(drop=True)


def add_lag_features(df: pd.DataFrame) -> pd.DataFrame:
    """Lags, rolling statistics and zero-fraction, computed per series."""

    grouped = df.groupby(KEY_COLS, sort=False)["y"]

    for lag in LAGS:
        df[f"lag_{lag}"] = grouped.shift(lag)

    shifted = grouped.shift(1)
    df["_shifted"] = shifted

    by_series = df.groupby(KEY_COLS, sort=False)["_shifted"]

    for window in ROLLING_WINDOWS:
        df[f"roll_mean_{window}"] = by_series.transform(
            lambda s, w=window: s.rolling(w, min_periods=1).mean()
        )

    for window in (7, 28):
        df[f"roll_std_{window}"] = by_series.transform(
            lambda s, w=window: s.rolling(w, min_periods=2).std()
        ).fillna(0.0)

    df["roll_max_28"] = by_series.transform(
        lambda s: s.rolling(28, min_periods=1).max()
    )
    df["zero_frac_28"] = by_series.transform(
        lambda s: s.eq(0).rolling(28, min_periods=1).mean()
    )

    return df.drop(columns=["_shifted"])


def classify_regime(values: np.ndarray) -> str:
    """ADI / CV-squared quadrant classification (spec §6.2).

    adi < 1.32, cv2 < 0.49  -> SMOOTH
    adi >= 1.32, cv2 < 0.49 -> INTERMITTENT
    adi < 1.32, cv2 >= 0.49 -> ERRATIC
    adi >= 1.32, cv2 >= 0.49 -> LUMPY
    """

    nonzero = values[values > 0]

    if len(nonzero) == 0:
        return "NO_DEMAND"

    adi = float(len(values)) / float(len(nonzero))

    if len(nonzero) > 1:
        mean = float(np.mean(nonzero))
        cv2 = float(np.var(nonzero) / (mean * mean)) if mean > 0 else 0.0
    else:
        cv2 = 0.0

    if adi < 1.32:
        return "SMOOTH" if cv2 < 0.49 else "ERRATIC"

    return "INTERMITTENT" if cv2 < 0.49 else "LUMPY"


def route_regime(regime: str, facility_row, component_code: str) -> str:
    """Facilities with a thalassaemia centre route PRBC to the calendar-aware
    path regardless of classification (spec §6.2) — those series are driven by a
    patient transfusion calendar, not by a demand distribution."""

    if (
        component_code == "PRBC"
        and facility_row is not None
        and bool(facility_row.get("has_thalassaemia_centre"))
    ):
        return "SCHEDULED"

    return regime
