"""Shortage risk and expiry rescue (spec §6.5, §7).

    python -m scripts.run_risk_rescue
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone

import pandas as pd
from sqlalchemy import func, insert, select

from config.settings import DEMO_DATE
from core import config, geo
from db.models import (
    BloodGroup,
    BloodUnit,
    Compatibility,
    Component,
    ExpiryRescue,
    Facility,
    Forecast,
    InventorySnapshot,
    ShortageRisk,
    new_id,
)
from db.session import SessionLocal, init_db
from engines.expiry import rescue
from engines.risk.shortage import build_risk_rows

DEMO_DATETIME = datetime.combine(DEMO_DATE, time(8, 0, 0), tzinfo=timezone.utc)

KEY_COLS = ["facility_id", "component_id", "blood_group_id"]

RISK_HORIZON_DAYS = int(config.get("risk.horizon_days", 14))
REPLENISHMENT_WINDOW_DAYS = 28
NEED_SCORE_HORIZON_DAYS = 7


def bulk_insert(session, model, rows, chunk_size=5000):
    for start in range(0, len(rows), chunk_size):
        session.execute(insert(model), rows[start : start + chunk_size])
        session.flush()


def load_reference(session):
    facilities = session.scalars(
        select(Facility).where(Facility.is_active.is_(True))
    ).all()
    components = session.scalars(select(Component)).all()
    groups = session.scalars(select(BloodGroup)).all()

    return facilities, components, groups


def load_units(session):
    frame = pd.read_sql(
        select(
            BloodUnit.id,
            BloodUnit.din,
            BloodUnit.facility_id,
            BloodUnit.component_id,
            BloodUnit.blood_group_id,
            BloodUnit.expires_at,
            BloodUnit.status,
            BloodUnit.screening_status,
            BloodUnit.cold_chain_breach_count,
        ).where(
            BloodUnit.status.in_(
                ["AVAILABLE", "RESERVED", "CROSSMATCHED", "QUARANTINE", "IN_TRANSIT"]
            )
        ),
        session.bind,
    )

    frame["expires_at"] = pd.to_datetime(frame["expires_at"], utc=True)
    frame = frame[frame["expires_at"] > DEMO_DATETIME].copy()

    frame["hours_left"] = (
        frame["expires_at"] - DEMO_DATETIME
    ).dt.total_seconds() / 3600.0
    frame["days_left"] = frame["hours_left"] / 24.0

    frame["usable"] = (frame["status"] == rescue.TRANSFERABLE_STATUS) & (
        frame["screening_status"] == rescue.TRANSFERABLE_SCREENING
    )

    return frame


def load_quantiles(session):
    frame = pd.read_sql(
        select(
            Forecast.facility_id,
            Forecast.component_id,
            Forecast.blood_group_id,
            Forecast.target_date,
            Forecast.p10,
            Forecast.p50,
            Forecast.p90,
        ),
        session.bind,
    )

    frame["target_date"] = pd.to_datetime(frame["target_date"]).dt.date

    quantiles: dict = defaultdict(dict)

    for row in frame.itertuples():
        quantiles[(row.facility_id, row.component_id, row.blood_group_id)][
            row.target_date
        ] = (float(row.p10), float(row.p50), float(row.p90))

    return quantiles


def load_replenishment(session):
    """Expected units received per day, from the facility's own recent history.

    Assuming zero replenishment over a 14-day horizon guarantees that every
    series eventually shows a certain stockout, which is what made 83% of risk
    rows CRITICAL.
    """

    window_start = DEMO_DATE - timedelta(days=REPLENISHMENT_WINDOW_DAYS)

    rows = session.execute(
        select(
            InventorySnapshot.facility_id,
            InventorySnapshot.component_id,
            InventorySnapshot.blood_group_id,
            func.sum(InventorySnapshot.units_collected),
        )
        .where(InventorySnapshot.snapshot_date >= window_start)
        .group_by(
            InventorySnapshot.facility_id,
            InventorySnapshot.component_id,
            InventorySnapshot.blood_group_id,
        )
    ).all()

    return {
        (row[0], row[1], row[2]): float(row[3] or 0.0) / REPLENISHMENT_WINDOW_DAYS
        for row in rows
    }


def load_compatibility(session, allow_override: bool):
    """(component, recipient) -> [(donor, rank)] and the inverse direction."""

    statement = select(
        Compatibility.component_id,
        Compatibility.recipient_group_id,
        Compatibility.donor_group_id,
        Compatibility.preference_rank,
        Compatibility.requires_override,
    ).where(Compatibility.is_compatible.is_(True))

    if not allow_override:
        statement = statement.where(Compatibility.requires_override.is_(False))

    donors_for_recipient = defaultdict(list)
    recipients_for_donor = defaultdict(list)

    for component_id, recipient_id, donor_id, rank, _ in session.execute(statement):
        donors_for_recipient[(component_id, recipient_id)].append(
            (donor_id, int(rank or 3))
        )
        recipients_for_donor[(component_id, donor_id)].append(
            (recipient_id, int(rank or 3))
        )

    return donors_for_recipient, recipients_for_donor


def build_need_scores(risk_rows, recipients_for_donor):
    """What a donor unit of group g would be worth at each facility.

    Spec §7.2 defines the recipient need score in terms of the recipient's
    shortage risk, not raw demand. A facility with plenty of stock and high
    throughput is not a good destination for a unit about to expire.
    """

    peak: dict = {}

    horizon_cutoff = NEED_SCORE_HORIZON_DAYS

    for row in risk_rows:
        if row["horizon_days"] > horizon_cutoff:
            continue

        key = (row["facility_id"], row["component_id"], row["blood_group_id"])
        value = float(row["shortage_probability"]) * float(row["required_p90"])

        if value > peak.get(key, 0.0):
            peak[key] = value

    need_scores: dict = {}

    for (facility_id, component_id, recipient_group_id), value in peak.items():
        if value <= 0:
            continue

        for donor_group_id, rank in recipients_for_donor.get(
            (component_id, recipient_group_id), []
        ):
            scored = value / float(max(1, rank))
            donor_key = (facility_id, component_id, donor_group_id)

            if scored > need_scores.get(donor_key, 0.0):
                need_scores[donor_key] = scored

    return need_scores


def build_rescue_rows(
    units,
    facilities,
    facilities_by_id,
    component_codes,
    component_by_id,
    quantiles,
    need_scores,
    travel_minutes,
    *,
    generated_at: datetime,
):
    run_id = new_id()
    facility_ids = [facility.id for facility in facilities]

    usable = units[units["usable"]].copy()
    usable = usable.sort_values(KEY_COLS + ["expires_at"])
    usable["queue_position"] = usable.groupby(KEY_COLS).cumcount() + 1
    queue_by_unit = dict(zip(usable["id"], usable["queue_position"]))

    rows = []

    for unit in units.itertuples():
        component_code = component_codes.get(unit.component_id, "PRBC")
        window_days = rescue.rescue_window_days(component_code)

        if unit.days_left > window_days:
            continue

        component = component_by_id.get(unit.component_id)
        max_transport_hours = float(
            component.max_transport_hours if component else 24.0
        )

        is_usable = bool(unit.usable)
        queue_position = int(queue_by_unit.get(unit.id, 1))

        # Consumption is only counted over the time the unit actually has left.
        consumable_days = int(max(0, min(unit.days_left, window_days)))
        series_quantiles = quantiles.get(
            (unit.facility_id, unit.component_id, unit.blood_group_id), {}
        )
        window_quantiles = [
            series_quantiles[DEMO_DATE + timedelta(days=offset)]
            for offset in range(consumable_days)
            if DEMO_DATE + timedelta(days=offset) in series_quantiles
        ]

        if is_usable:
            probability = rescue.waste_probability(queue_position, window_quantiles)
        else:
            # Allocated to a patient or not yet released. Not at risk of expiry
            # in the sense this engine means, and not transferable either.
            probability = 0.0

        best = None

        if is_usable and int(unit.cold_chain_breach_count or 0) == 0:
            best = rescue.find_best_recipient(
                unit={
                    "facility_id": unit.facility_id,
                    "component_id": unit.component_id,
                    "blood_group_id": unit.blood_group_id,
                },
                facility_ids=facility_ids,
                travel_minutes=travel_minutes,
                need_score=need_scores,
                max_transport_hours=max_transport_hours,
                hours_left=float(unit.hours_left),
            )

        tier, reason = rescue.classify(
            is_transferable_status=is_usable,
            has_cold_chain_breach=int(unit.cold_chain_breach_count or 0) > 0,
            hours_left=float(unit.hours_left),
            probability=probability,
            best_recipient=best,
        )

        recipient_id, minutes, need_score = best if best else (None, None, 0.0)
        recipient = facilities_by_id.get(recipient_id) if recipient_id else None

        criticality = float(component.criticality_weight if component else 1.0)

        dispatch_deadline_at = None
        hours_to_deadline = None

        if minutes is not None:
            lead_hours = minutes / 60.0 + rescue.handling_buffer_hours()
            dispatch_deadline_at = unit.expires_at - timedelta(hours=lead_hours)
            hours_to_deadline = float(unit.hours_left) - lead_hours

        rows.append(
            {
                "id": new_id(),
                "run_id": run_id,
                "blood_unit_id": unit.id,
                "facility_id": unit.facility_id,
                "component_id": unit.component_id,
                "blood_group_id": unit.blood_group_id,
                "expires_at": unit.expires_at,
                "days_left": float(unit.days_left),
                "waste_probability": float(probability),
                "rescue_tier": tier,
                "transferable": bool(tier in {"ACT_NOW", "WATCH"}),
                "best_recipient_facility_id": recipient_id,
                "best_travel_minutes": minutes,
                "dispatch_deadline_at": dispatch_deadline_at,
                "hours_to_deadline": hours_to_deadline,
                "best_need_score": float(need_score),
                "rescue_value": float(probability * criticality * need_score),
                "reason_en": rescue.build_reason(
                    tier,
                    reason,
                    float(unit.hours_left),
                    recipient.name_en if recipient else None,
                    minutes,
                ),
                "reason_ur": None,
                "generated_at": generated_at,
            }
        )

    return rows


def rebuild(session, *, generated_at: datetime | None = None) -> dict:
    """Replace live risk/rescue rows without committing the caller's transaction."""

    generated_at = generated_at or datetime.now(timezone.utc)
    facilities, components, groups = load_reference(session)
    facilities_by_id = {facility.id: facility for facility in facilities}
    component_by_id = {component.id: component for component in components}
    component_codes = {component.id: component.code for component in components}
    group_codes = {group.id: group.code for group in groups}

    units = load_units(session)
    quantiles = load_quantiles(session)
    replenishment = load_replenishment(session)
    travel_minutes = geo.build_travel_matrix(facilities)

    usable = units[units["usable"]]
    on_hand = usable.groupby(KEY_COLS).size().to_dict()
    expiry_dates = {
        key: sorted(group["expires_at"].dt.date.tolist())
        for key, group in usable.groupby(KEY_COLS)
    }
    risk_rows = build_risk_rows(
        demo_date=DEMO_DATE,
        horizon_days=RISK_HORIZON_DAYS,
        facilities_by_id=facilities_by_id,
        component_codes=component_codes,
        group_codes=group_codes,
        on_hand=on_hand,
        expiry_dates=expiry_dates,
        quantiles=quantiles,
        replenishment=replenishment,
        generated_at=generated_at,
        new_id=new_id,
    )

    allow_override = bool(config.get("optimizer.allow_override_compatibility", False))
    _, recipients_for_donor = load_compatibility(session, allow_override)
    need_scores = build_need_scores(risk_rows, recipients_for_donor)
    rescue_rows = build_rescue_rows(
        units,
        facilities,
        facilities_by_id,
        component_codes,
        component_by_id,
        quantiles,
        need_scores,
        travel_minutes,
        generated_at=generated_at,
    )
    suggestions = rescue.prevention_suggestions(
        rescue_rows, facilities_by_id, component_codes, group_codes
    )

    session.query(ShortageRisk).delete(synchronize_session=False)
    session.query(ExpiryRescue).delete(synchronize_session=False)
    session.flush()
    bulk_insert(session, ShortageRisk, risk_rows)
    bulk_insert(session, ExpiryRescue, rescue_rows)
    session.flush()

    buckets = Counter(row["risk_bucket"] for row in risk_rows)
    tiers = Counter(row["rescue_tier"] for row in rescue_rows)
    return {
        "risk_rows": len(risk_rows),
        "rescue_rows": len(rescue_rows),
        "critical_risk_rows": int(buckets.get("CRITICAL", 0)),
        "actionable_rescue_rows": int(
            tiers.get("ACT_NOW", 0) + tiers.get("WATCH", 0)
        ),
        "prevention_suggestions": len(suggestions),
    }


def main():
    init_db()
    session = SessionLocal()

    try:
        print("Refreshing shortage risk and expiry rescue...")
        result = rebuild(session)
        session.commit()
        print("Risk and rescue complete.")
        for key, value in result.items():
            print(f"  {key}: {value:,}" if isinstance(value, int) else f"  {key}: {value}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
