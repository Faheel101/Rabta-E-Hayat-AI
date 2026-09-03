"""Decision-ready reads for the Command Centre and intelligence pages.

The forecasting, risk and expiry engines persist their outputs independently.
This service is the product contract above those tables: tenant-scoped facts,
deterministic priorities, honest model-quality gates and chart-ready data.  It
contains no writes; a recommendation only becomes an instruction in the
transfer workflow.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from core import config
from core.clock import DEMO_DATETIME
from db.models import (
    BloodGroup,
    BloodUnit,
    Component,
    ExpiryRescue,
    Facility,
    Forecast,
    ForecastMetric,
    ForecastRunSummary,
    MartDailyDemand,
    MartDaysOfCover,
    MartFacilityKpi,
    ShortageRisk,
    Transfer,
)

RISK_RANK = {"SAFE": 0, "WATCH": 1, "WARNING": 2, "CRITICAL": 3}
TIER_RANK = {
    "SAFE": 0,
    "NOT_TRANSFERABLE": 1,
    "UNRESCUABLE": 2,
    "WATCH": 3,
    "ACT_NOW": 4,
}
COST_PER_UNIT = float(config.get("impact.cost_per_unit_pkr", 15000))


def _percent(value: float | None) -> float | None:
    """Normalise stored ratios and already-percent metrics for presentation."""

    if value is None:
        return None
    value = float(value)
    return round(value * 100 if abs(value) <= 1 else value, 1)


def _points(values: list[float], *, width: int, height: int, maximum: float) -> str:
    if not values:
        return ""

    maximum = max(maximum, 1.0)
    count = max(len(values) - 1, 1)
    return " ".join(
        f"{index / count * width:.1f},{height - value / maximum * height:.1f}"
        for index, value in enumerate(values)
    )


def _area_points(
    lower: list[float], upper: list[float], *, width: int, height: int, maximum: float
) -> str:
    if not lower or not upper:
        return ""
    return " ".join(
        [
            _points(upper, width=width, height=height, maximum=maximum),
            " ".join(
                reversed(
                    _points(lower, width=width, height=height, maximum=maximum).split()
                )
            ),
        ]
    )


def _ticks(dates: list[date], *, width: int, count: int = 5) -> list[dict]:
    if not dates:
        return []

    indexes = sorted(
        {
            round(i * (len(dates) - 1) / max(count - 1, 1))
            for i in range(min(count, len(dates)))
        }
    )
    denominator = max(len(dates) - 1, 1)
    return [
        {
            "x": round(index / denominator * width, 1),
            "label": dates[index].strftime("%d %b"),
        }
        for index in indexes
    ]


def reference_options(db: Session) -> dict:
    components = [
        {"id": row.id, "code": row.code, "name": row.name_en}
        for row in db.scalars(select(Component).order_by(Component.id)).all()
    ]
    groups = [
        {"id": row.id, "code": row.code}
        for row in db.scalars(select(BloodGroup).order_by(BloodGroup.id)).all()
    ]
    return {"components": components, "groups": groups}


def _worst_shortage_rows(db: Session, facility_ids: list[str]) -> list[dict]:
    if not facility_ids:
        return []

    rows = db.execute(
        select(
            ShortageRisk.facility_id,
            Facility.name_en.label("facility_name"),
            ShortageRisk.component_id,
            Component.code.label("component_code"),
            ShortageRisk.blood_group_id,
            BloodGroup.code.label("group_code"),
            ShortageRisk.risk_date,
            ShortageRisk.projected_available,
            ShortageRisk.reserve_floor,
            ShortageRisk.shortage_probability,
            ShortageRisk.risk_bucket,
        )
        .join(Facility, Facility.id == ShortageRisk.facility_id)
        .join(Component, Component.id == ShortageRisk.component_id)
        .join(BloodGroup, BloodGroup.id == ShortageRisk.blood_group_id)
        .where(ShortageRisk.facility_id.in_(facility_ids))
        .order_by(ShortageRisk.risk_date)
    ).all()

    series: dict[tuple[str, int, int], dict] = {}
    for row in rows:
        key = (row.facility_id, row.component_id, row.blood_group_id)
        candidate = series.get(key)
        rank = RISK_RANK.get(row.risk_bucket, 0)

        if candidate is None:
            candidate = {
                "facility_id": row.facility_id,
                "facility_name": row.facility_name,
                "component_id": row.component_id,
                "component_code": row.component_code,
                "blood_group_id": row.blood_group_id,
                "group_code": row.group_code,
                "risk_bucket": row.risk_bucket,
                "shortage_probability": float(row.shortage_probability or 0),
                "first_breach": None,
                "stockout_date": None,
                "projected_available": float(row.projected_available or 0),
                "reserve_floor": float(row.reserve_floor or 0),
            }
            series[key] = candidate

        if (
            rank > RISK_RANK.get(candidate["risk_bucket"], 0)
            or (
                rank == RISK_RANK.get(candidate["risk_bucket"], 0)
                and float(row.shortage_probability or 0)
                > candidate["shortage_probability"]
            )
        ):
            candidate.update(
                risk_bucket=row.risk_bucket,
                shortage_probability=float(row.shortage_probability or 0),
                projected_available=float(row.projected_available or 0),
                reserve_floor=float(row.reserve_floor or 0),
            )

        if (
            candidate["first_breach"] is None
            and float(row.projected_available or 0) <= float(row.reserve_floor or 0)
        ):
            candidate["first_breach"] = row.risk_date
        if candidate["stockout_date"] is None and float(row.projected_available or 0) <= 0:
            candidate["stockout_date"] = row.risk_date

    return sorted(
        series.values(),
        key=lambda item: (
            -RISK_RANK.get(item["risk_bucket"], 0),
            -item["shortage_probability"],
            item["first_breach"] or date.max,
        ),
    )


def model_quality(db: Session) -> dict:
    row = db.scalar(
        select(ForecastRunSummary).order_by(ForecastRunSummary.generated_at.desc())
    )
    if row is None:
        return {"available": False, "gates_passed": 0, "gates_total": 4}

    detail = row.metrics_json or {}
    decision_wape = _percent(detail.get("wape_component_grain_7d"))
    group_wape = _percent(row.wape_dense_7d)
    noise_floor = _percent(row.noise_floor_dense_7d)
    beats_naive = _percent(row.pct_series_beating_naive)
    coverage = _percent(row.picp_p10_p90)
    recall = _percent(row.shortage_detection_recall_3d)
    gates = {
        "wape": decision_wape is not None and decision_wape <= 25,
        "beats_naive": beats_naive is not None and beats_naive >= 80,
        "coverage": coverage is not None and 75 <= coverage <= 85,
        "recall": recall is not None and recall >= 75,
    }
    return {
        "available": True,
        "generated_at": row.generated_at,
        "series_total": row.series_total,
        "series_dense": row.series_dense,
        "series_fallback": row.series_fallback,
        "decision_wape": decision_wape,
        "group_wape": group_wape,
        "noise_floor": noise_floor,
        "beats_naive": beats_naive,
        "coverage": coverage,
        "recall": recall,
        "gates": gates,
        "gates_passed": sum(gates.values()),
        "gates_total": len(gates),
    }


def command_centre(
    db: Session, facility_ids: list[str], heatmap_facility_id: str
) -> dict:
    """One morning-read payload for a facility or organization scope."""

    risks = _worst_shortage_rows(db, facility_ids)
    alerts = [r for r in risks if r["risk_bucket"] in {"WARNING", "CRITICAL"}]
    critical = [r for r in alerts if r["risk_bucket"] == "CRITICAL"]
    health = 100.0 * (1 - len(alerts) / len(risks)) if risks else 100.0

    rescue_rows = db.execute(
        select(ExpiryRescue.rescue_tier, ExpiryRescue.days_left).where(
            ExpiryRescue.facility_id.in_(facility_ids),
            ExpiryRescue.days_left <= 3,
        )
    ).all()
    expiring_72h = len(rescue_rows)
    expiry_critical = sum(row.rescue_tier == "ACT_NOW" for row in rescue_rows)

    pending_transfers = db.scalar(
        select(func.count())
        .select_from(Transfer)
        .where(
            Transfer.status == "RECOMMENDED",
            or_(
                Transfer.from_facility_id.in_(facility_ids),
                Transfer.to_facility_id.in_(facility_ids),
            ),
        )
    ) or 0

    month_start = DEMO_DATETIME.replace(day=1, hour=0, minute=0, second=0)
    rescued_mtd = db.scalar(
        select(func.coalesce(func.sum(Transfer.units), 0)).where(
            Transfer.status.in_(["APPROVED", "DISPATCHED", "IN_TRANSIT", "RECEIVED"]),
            Transfer.from_facility_id.in_(facility_ids),
            Transfer.approved_at >= month_start,
        )
    ) or 0

    recipient = aliased(Facility)
    expiry_actions = db.execute(
        select(
            ExpiryRescue.blood_unit_id,
            BloodUnit.din,
            Facility.name_en.label("facility_name"),
            Component.code.label("component_code"),
            BloodGroup.code.label("group_code"),
            ExpiryRescue.rescue_tier,
            ExpiryRescue.hours_to_deadline,
            ExpiryRescue.waste_probability,
            ExpiryRescue.best_travel_minutes,
            recipient.name_en.label("destination_name"),
            ExpiryRescue.reason_en,
            ExpiryRescue.reason_ur,
        )
        .join(BloodUnit, BloodUnit.id == ExpiryRescue.blood_unit_id)
        .join(Facility, Facility.id == ExpiryRescue.facility_id)
        .join(Component, Component.id == ExpiryRescue.component_id)
        .join(BloodGroup, BloodGroup.id == ExpiryRescue.blood_group_id)
        .outerjoin(recipient, recipient.id == ExpiryRescue.best_recipient_facility_id)
        .where(
            ExpiryRescue.facility_id.in_(facility_ids),
            ExpiryRescue.rescue_tier.in_(["ACT_NOW", "WATCH"]),
        )
        .order_by(
            ExpiryRescue.hours_to_deadline.is_(None),
            ExpiryRescue.hours_to_deadline,
            ExpiryRescue.waste_probability.desc(),
        )
        .limit(4)
    ).all()

    references = reference_options(db)
    group_order = [group["code"] for group in references["groups"]]
    cover_rows = db.execute(
        select(
            Component.id.label("component_id"),
            Component.code.label("component_code"),
            BloodGroup.id.label("blood_group_id"),
            BloodGroup.code.label("group_code"),
            MartDaysOfCover.units_available,
            MartDaysOfCover.days_of_cover,
            MartDaysOfCover.avg_daily_demand,
            MartDaysOfCover.risk_bucket,
        )
        .join(Component, Component.id == MartDaysOfCover.component_id)
        .join(BloodGroup, BloodGroup.id == MartDaysOfCover.blood_group_id)
        .where(MartDaysOfCover.facility_id == heatmap_facility_id)
        .order_by(Component.id, BloodGroup.id)
    ).all()

    heatmap_by_component: dict[tuple[int, str], dict] = {}
    for row in cover_rows:
        key = (row.component_id, row.component_code)
        component = heatmap_by_component.setdefault(
            key, {"component_id": row.component_id, "code": row.component_code, "cells": {}}
        )
        component["cells"][row.group_code] = {
            "blood_group_id": row.blood_group_id,
            "units": row.units_available,
            "days_of_cover": row.days_of_cover,
            "avg_daily_demand": row.avg_daily_demand,
            "risk_bucket": row.risk_bucket,
        }

    heatmap = []
    for component in heatmap_by_component.values():
        component["cells"] = [
            {
                "group_code": code,
                **component["cells"].get(
                    code,
                    {
                        "blood_group_id": None,
                        "units": 0,
                        "days_of_cover": None,
                        "avg_daily_demand": 0,
                        "risk_bucket": "NO_DEMAND",
                    },
                ),
            }
            for code in group_order
        ]
        heatmap.append(component)

    feed_rows = db.execute(
        select(
            MartFacilityKpi.name_en,
            MartFacilityKpi.feed_status,
            MartFacilityKpi.feed_age_hours,
        ).where(MartFacilityKpi.facility_id.in_(facility_ids))
    ).all()

    return {
        "summary": {
            "network_health_pct": round(health, 1),
            "shortage_alerts": len(alerts),
            "shortage_critical": len(critical),
            "expiring_72h": expiring_72h,
            "expiry_critical": expiry_critical,
            "pending_transfers": int(pending_transfers),
            "rescued_mtd": int(rescued_mtd),
        },
        "shortage_actions": alerts[:5],
        "expiry_actions": [row._asdict() for row in expiry_actions],
        "heatmap_groups": group_order,
        "heatmap": heatmap,
        "quality": model_quality(db),
        "feeds": {
            "healthy": sum(row.feed_status == "HEALTHY" for row in feed_rows),
            "total": len(facility_ids),
            "stale": [row._asdict() for row in feed_rows if row.feed_status != "HEALTHY"],
        },
    }


def _forecast_chart(history: list, forecasts: list) -> dict:
    history = history[-42:]
    dates = [row.demand_date for row in history] + [row.target_date for row in forecasts]
    actual = [float(row.units_requested or 0) for row in history]
    p10 = [float(row.p10 or 0) for row in forecasts]
    p50 = [float(row.p50 or 0) for row in forecasts]
    p90 = [float(row.p90 or 0) for row in forecasts]
    maximum = max(actual + p90 + [1.0]) * 1.12
    width, height = 920, 250
    history_width = width * (len(history) - 1) / max(len(dates) - 1, 1)
    forecast_width = width * (len(forecasts) - 1) / max(len(dates) - 1, 1)
    start_x = width * len(history) / max(len(dates) - 1, 1)

    def shifted(points: str) -> str:
        output = []
        for point in points.split():
            x, y = point.split(",")
            output.append(f"{float(x) + start_x:.1f},{y}")
        return " ".join(output)

    forecast_area = _area_points(
        p10, p90, width=forecast_width, height=height, maximum=maximum
    )
    return {
        "width": width,
        "height": height,
        "actual_points": _points(
            actual, width=history_width, height=height, maximum=maximum
        ),
        "p50_points": shifted(
            _points(p50, width=forecast_width, height=height, maximum=maximum)
        ),
        "band_points": shifted(forecast_area),
        "today_x": round(start_x, 1),
        "maximum": maximum,
        "y_ticks": [
            {"value": round(maximum * fraction, 1), "y": round(height * (1 - fraction), 1)}
            for fraction in (0, 0.5, 1)
        ],
        "x_ticks": _ticks(dates, width=width),
    }


def _stock_chart(rows: list) -> dict:
    values = [float(row.projected_available or 0) for row in rows]
    floors = [float(row.reserve_floor or 0) for row in rows]
    maximum = max(values + floors + [1.0]) * 1.12
    width, height = 920, 150
    return {
        "width": width,
        "height": height,
        "available_points": _points(values, width=width, height=height, maximum=maximum),
        "floor_points": _points(floors, width=width, height=height, maximum=maximum),
        "x_ticks": _ticks([row.risk_date for row in rows], width=width, count=4),
        "y_ticks": [
            {"value": round(maximum * fraction, 1), "y": round(height * (1 - fraction), 1)}
            for fraction in (0, 0.5, 1)
        ],
    }


def forecast_detail(
    db: Session,
    facility_id: str,
    component_id: int | None,
    blood_group_id: int | None,
    horizon: int,
) -> dict:
    references = reference_options(db)
    available = db.execute(
        select(
            MartDaysOfCover.component_id,
            MartDaysOfCover.blood_group_id,
            Component.code.label("component_code"),
            BloodGroup.code.label("group_code"),
            MartDaysOfCover.units_available,
            MartDaysOfCover.days_of_cover,
            MartDaysOfCover.risk_bucket,
            MartDaysOfCover.shortage_probability,
        )
        .join(Component, Component.id == MartDaysOfCover.component_id)
        .join(BloodGroup, BloodGroup.id == MartDaysOfCover.blood_group_id)
        .where(MartDaysOfCover.facility_id == facility_id)
        .order_by(
            MartDaysOfCover.shortage_probability.desc(), Component.id, BloodGroup.id
        )
    ).all()
    if not available:
        return {"references": references, "series": [], "selected": None}

    valid = {(row.component_id, row.blood_group_id) for row in available}
    selected_key = (component_id, blood_group_id)
    if selected_key not in valid:
        component_id, blood_group_id = available[0].component_id, available[0].blood_group_id

    history = db.execute(
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
        )
        .order_by(MartDailyDemand.demand_date.desc())
        .limit(120)
    ).all()[::-1]
    forecasts = db.execute(
        select(
            Forecast.target_date,
            Forecast.horizon_days,
            Forecast.p10,
            Forecast.p50,
            Forecast.p90,
            Forecast.model_version,
            Forecast.generated_at,
        )
        .where(
            Forecast.facility_id == facility_id,
            Forecast.component_id == component_id,
            Forecast.blood_group_id == blood_group_id,
            Forecast.horizon_days <= horizon,
        )
        .order_by(Forecast.target_date)
    ).all()
    projection = db.execute(
        select(
            ShortageRisk.risk_date,
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
            ShortageRisk.horizon_days <= min(horizon, 14),
        )
        .order_by(ShortageRisk.risk_date)
    ).all()

    metric = db.scalar(
        select(ForecastMetric)
        .where(
            ForecastMetric.facility_id == facility_id,
            ForecastMetric.component_id == component_id,
            ForecastMetric.blood_group_id == blood_group_id,
            ForecastMetric.horizon_days == 7,
        )
        .order_by(ForecastMetric.generated_at.desc())
    )
    diagnostics = None
    if metric:
        skill = None
        if metric.wape is not None and metric.baseline_seasonal_naive_wape:
            skill = 100 * (1 - metric.wape / metric.baseline_seasonal_naive_wape)
        diagnostics = {
            "model": metric.model_version,
            "regime": metric.regime,
            "wape": _percent(metric.wape),
            "baseline_wape": _percent(metric.baseline_seasonal_naive_wape),
            "skill": round(skill, 1) if skill is not None else None,
            "noise_floor": _percent(metric.wape_noise_floor),
            "pinball_p90": metric.pinball_p90,
            "coverage": _percent(metric.picp),
            "is_fallback": bool(metric.is_fallback),
            "generated_at": metric.generated_at,
        }

    breach = next(
        (
            row.risk_date
            for row in projection
            if float(row.projected_available or 0) <= float(row.reserve_floor or 0)
        ),
        None,
    )
    stockout = next(
        (row.risk_date for row in projection if float(row.projected_available or 0) <= 0),
        None,
    )

    selected = next(
        row for row in available if (row.component_id, row.blood_group_id) == (component_id, blood_group_id)
    )
    compare = []
    for row in available:
        compare.append(
            {
                **row._asdict(),
                "risk_pct": _percent(row.shortage_probability),
            }
        )

    return {
        "references": references,
        "series": compare,
        "selected": selected._asdict(),
        "component_id": component_id,
        "blood_group_id": blood_group_id,
        "history": [row._asdict() for row in history],
        "forecasts": [row._asdict() for row in forecasts],
        "projection": [row._asdict() for row in projection],
        "forecast_chart": _forecast_chart(history, forecasts),
        "stock_chart": _stock_chart(projection),
        "diagnostics": diagnostics,
        "breach_date": breach,
        "stockout_date": stockout,
        "quality": model_quality(db),
    }


def expiry_rescue(
    db: Session,
    facility_ids: list[str],
    *,
    facility_id: str | None = None,
    component_id: int | None = None,
    tier: str | None = None,
    sort_by: str = "deadline",
    page: int = 1,
    page_size: int = 12,
) -> dict:
    """Filtered rescue work queue with reasons and structural prevention."""

    scoped_ids = [facility_id] if facility_id else facility_ids
    base_conditions = [ExpiryRescue.facility_id.in_(scoped_ids)]
    if component_id:
        base_conditions.append(ExpiryRescue.component_id == component_id)

    recipient = aliased(Facility)
    row_conditions = list(base_conditions)
    if tier == "ACTIONABLE":
        row_conditions.append(ExpiryRescue.rescue_tier.in_(["ACT_NOW", "WATCH"]))
    elif tier:
        row_conditions.append(ExpiryRescue.rescue_tier == tier)

    # Join recommendations to their governed execution record in memory. A
    # rescue score is only evidence; the Transfer is the human approval gate.
    # Building this map once avoids an N+1 query for every unit in the queue.
    active_transfers = db.scalars(
        select(Transfer)
        .where(
            Transfer.from_facility_id.in_(scoped_ids),
            Transfer.status.in_(["RECOMMENDED", "APPROVED", "DISPATCHED", "IN_TRANSIT"]),
        )
        .order_by(Transfer.created_at.desc())
    ).all()
    transfer_by_unit: dict[str, Transfer] = {}
    for transfer in active_transfers:
        for unit_id in transfer.unit_ids or []:
            transfer_by_unit.setdefault(str(unit_id), transfer)
    destination_names = dict(
        db.execute(
            select(Facility.id, Facility.name_en).where(
                Facility.id.in_(
                    {transfer.to_facility_id for transfer in active_transfers}
                    or {"__none__"}
                )
            )
        ).all()
    )

    statement = (
        select(
            ExpiryRescue.blood_unit_id,
            BloodUnit.din,
            ExpiryRescue.facility_id,
            Facility.name_en.label("facility_name"),
            ExpiryRescue.component_id,
            Component.code.label("component_code"),
            BloodGroup.code.label("group_code"),
            ExpiryRescue.expires_at,
            ExpiryRescue.days_left,
            ExpiryRescue.waste_probability,
            ExpiryRescue.rescue_tier,
            ExpiryRescue.transferable,
            ExpiryRescue.best_recipient_facility_id,
            recipient.name_en.label("destination_name"),
            ExpiryRescue.best_travel_minutes,
            ExpiryRescue.dispatch_deadline_at,
            ExpiryRescue.hours_to_deadline,
            ExpiryRescue.rescue_value,
            ExpiryRescue.reason_en,
            ExpiryRescue.reason_ur,
        )
        .join(BloodUnit, BloodUnit.id == ExpiryRescue.blood_unit_id)
        .join(Facility, Facility.id == ExpiryRescue.facility_id)
        .join(Component, Component.id == ExpiryRescue.component_id)
        .join(BloodGroup, BloodGroup.id == ExpiryRescue.blood_group_id)
        .outerjoin(recipient, recipient.id == ExpiryRescue.best_recipient_facility_id)
        .where(*row_conditions)
    )
    if sort_by == "probability":
        statement = statement.order_by(ExpiryRescue.waste_probability.desc())
    elif sort_by == "value":
        statement = statement.order_by(ExpiryRescue.rescue_value.desc())
    else:
        statement = statement.order_by(
            ExpiryRescue.hours_to_deadline.is_(None),
            ExpiryRescue.hours_to_deadline,
        )
    filtered_total = db.scalar(
        select(func.count()).select_from(ExpiryRescue).where(*row_conditions)
    ) or 0
    page_size = max(10, min(50, int(page_size)))
    pages = max(1, (int(filtered_total) + page_size - 1) // page_size)
    page = max(1, min(int(page), pages))
    rows = db.execute(
        statement.offset((page - 1) * page_size).limit(page_size)
    ).all()

    queue_rows = []
    for row in rows:
        item = row._asdict()
        linked_transfer = transfer_by_unit.get(str(row.blood_unit_id))
        planned_deadline = row.dispatch_deadline_at
        if (
            linked_transfer
            and planned_deadline
            and row.best_travel_minutes is not None
            and linked_transfer.est_travel_minutes is not None
        ):
            # Preserve the engine's handling buffer while replacing the
            # single-unit candidate travel time with the governed plan route.
            planned_deadline += timedelta(
                minutes=row.best_travel_minutes - linked_transfer.est_travel_minutes
            )
        item.update(
            {
                "transfer_id": linked_transfer.id if linked_transfer else None,
                "transfer_status": linked_transfer.status if linked_transfer else None,
                "transfer_tracking_code": (
                    linked_transfer.tracking_code if linked_transfer else None
                ),
                "display_destination_name": (
                    destination_names.get(linked_transfer.to_facility_id)
                    if linked_transfer
                    else row.destination_name
                ),
                "display_travel_minutes": (
                    linked_transfer.est_travel_minutes
                    if linked_transfer
                    else row.best_travel_minutes
                ),
                "display_dispatch_deadline_at": (
                    planned_deadline if linked_transfer else row.dispatch_deadline_at
                ),
            }
        )
        queue_rows.append(item)

    summary_rows = db.execute(
        select(
            ExpiryRescue.blood_unit_id,
            ExpiryRescue.rescue_tier,
            ExpiryRescue.days_left,
            ExpiryRescue.rescue_value,
        ).where(*base_conditions)
    ).all()
    within_7d = [row for row in summary_rows if row.days_left <= 7]
    at_risk = [
        row
        for row in within_7d
        if row.rescue_tier in {"ACT_NOW", "WATCH", "UNRESCUABLE"}
    ]
    rescuable = [row for row in at_risk if row.rescue_tier in {"ACT_NOW", "WATCH"}]
    unrescuable = [row for row in at_risk if row.rescue_tier == "UNRESCUABLE"]
    approval_ready_transfers = {
        transfer.id
        for row in rescuable
        if (transfer := transfer_by_unit.get(str(row.blood_unit_id)))
        and transfer.status == "RECOMMENDED"
    }

    month_start = DEMO_DATETIME.replace(day=1, hour=0, minute=0, second=0)
    rescued_mtd = db.scalar(
        select(func.coalesce(func.sum(Transfer.units), 0)).where(
            Transfer.status.in_(["APPROVED", "DISPATCHED", "IN_TRANSIT", "RECEIVED"]),
            Transfer.from_facility_id.in_(scoped_ids),
            Transfer.approved_at >= month_start,
        )
    ) or 0

    timeline_rows = db.execute(
        select(ExpiryRescue.expires_at, ExpiryRescue.rescue_tier).where(
            *base_conditions,
            ExpiryRescue.days_left >= 0,
            ExpiryRescue.days_left <= 14,
        )
    ).all()
    timeline_by_date: dict[date, dict] = defaultdict(lambda: {"at_risk": 0, "used": 0})
    for row in timeline_rows:
        key = row.expires_at.date()
        if row.rescue_tier in {"ACT_NOW", "WATCH", "UNRESCUABLE"}:
            timeline_by_date[key]["at_risk"] += 1
        else:
            timeline_by_date[key]["used"] += 1
    timeline = [
        {"date": day, **counts, "total": counts["at_risk"] + counts["used"]}
        for day, counts in sorted(timeline_by_date.items())
    ]
    timeline_max = max([row["total"] for row in timeline] + [1])
    for row in timeline:
        row["at_risk_pct"] = round(100 * row["at_risk"] / timeline_max, 1)
        row["used_pct"] = round(100 * row["used"] / timeline_max, 1)

    prevention_rows = db.execute(
        select(
            Facility.id.label("facility_id"),
            Facility.name_en.label("facility_name"),
            Component.code.label("component_code"),
            BloodGroup.code.label("group_code"),
            func.count().label("units"),
        )
        .select_from(ExpiryRescue)
        .join(Facility, Facility.id == ExpiryRescue.facility_id)
        .join(Component, Component.id == ExpiryRescue.component_id)
        .join(BloodGroup, BloodGroup.id == ExpiryRescue.blood_group_id)
        .where(
            *base_conditions,
            ExpiryRescue.rescue_tier == "UNRESCUABLE",
        )
        .group_by(Facility.id, Component.id, BloodGroup.id)
        .having(func.count() >= 3)
        .order_by(func.count().desc())
        .limit(8)
    ).all()

    return {
        "rows": queue_rows,
        "filtered_total": int(filtered_total),
        "page": page,
        "pages": pages,
        "summary": {
            "at_risk": len(at_risk),
            "rescuable": len(rescuable),
            "unrescuable": len(unrescuable),
            "rescued_mtd": int(rescued_mtd),
            "approval_ready_transfers": len(approval_ready_transfers),
            # rescue_value is an optimizer score, not money. Currency uses the
            # configured replacement cost so the label cannot misstate a
            # dimensionless model signal as rupees.
            "value_at_risk": len(at_risk) * COST_PER_UNIT,
        },
        "timeline": timeline,
        "prevention": [row._asdict() for row in prevention_rows],
        "references": reference_options(db),
    }
