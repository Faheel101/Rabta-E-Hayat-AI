"""Shortage risk and safety stock (spec §6.5).

The previous implementation compared on-hand stock against demand accumulated
from today to each future day, with no replenishment. Two consequences followed
mechanically: every series ran out by the end of the horizon, and 83% of all
risk rows came back CRITICAL — 906 of 1,440 series critical on day one. A risk
score that fires on everything conveys nothing, and it is precisely how the
alert fatigue described in spec §11.2 kills a system in month three.

Two things were wrong. The reserve floor was a component-level number applied to
each of eight blood group series, so a 40-unit PRBC policy demanded 320 units of
reserve; that is fixed in core/policy.py. And the requirement was cumulative
rather than the rolling lead-time window the spec actually defines:

    required_stock(d)  = sum of P90 demand over [d, d + L] + reserve_floor
    projected_stock(d) = on_hand - consumption - expiring + replenishment + inbound
    shortage_risk(d)   = P(demand > available), from the quantile spread

L is the lead time to receive from the parent RBC. Replenishment is the
facility's own recent collection rate: a facility collecting at its consumption
rate holds a roughly stationary position, and one collecting below it declines —
which is the signal worth surfacing, rather than an artefact of assuming nobody
ever restocks anything.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import date, timedelta

from core import config, demand_dist, policy


def lead_time_for(facility) -> int:
    return policy.lead_time_days(facility)


def build_risk_rows(
    *,
    demo_date: date,
    horizon_days: int,
    facilities_by_id: dict,
    component_codes: dict,
    group_codes: dict,
    on_hand: dict,
    expiry_dates: dict,
    quantiles: dict,
    replenishment: dict,
    inbound: dict | None = None,
    generated_at=None,
    new_id=None,
):
    """One row per (series, risk_date).

    `quantiles[key][target_date]` is a (p10, p50, p90) triple.
    `expiry_dates[key]` is a sorted list of expiry dates for usable stock.
    `replenishment[key]` is expected units received per day.
    """

    inbound = inbound or {}
    rows = []

    risk_dates = [demo_date + timedelta(days=offset) for offset in range(horizon_days)]

    series_keys = set(on_hand) | set(quantiles) | set(expiry_dates)

    for key in series_keys:
        facility_id, component_id, blood_group_id = key

        facility = facilities_by_id.get(facility_id)

        if facility is None:
            continue

        component_code = component_codes.get(component_id, "PRBC")
        group_code = group_codes.get(blood_group_id)

        reserve = (
            policy.reserve_floor(facility, component_code, group_code)
            if group_code
            else 0.0
        )

        lead_time = lead_time_for(facility)
        thresholds = demand_dist.risk_thresholds(component_code)

        starting_stock = float(on_hand.get(key, 0))
        expiries = expiry_dates.get(key, [])
        series_quantiles = quantiles.get(key, {})
        daily_replenishment = float(replenishment.get(key, 0.0))

        consumed = 0.0

        for horizon_offset, risk_date in enumerate(risk_dates, start=1):
            # Stock position at the start of risk_date.
            expired_by_now = float(bisect_left(expiries, risk_date))
            received = daily_replenishment * (horizon_offset - 1)
            arriving = float(inbound.get((key, risk_date), 0.0))

            projected_available = (
                starting_stock - consumed - expired_by_now + received + arriving
            )

            # Requirement over the lead-time window starting at risk_date: what
            # must be on hand now to survive until an order placed today lands.
            window = [
                risk_date + timedelta(days=offset)
                for offset in range(lead_time + 1)
            ]
            window_quantiles = [
                series_quantiles[day] for day in window if day in series_quantiles
            ]

            mean, sigma = demand_dist.window_moments(window_quantiles)

            required_p50 = mean + reserve

            # The 90th percentile of the WINDOW total, from the window's own
            # moments. Summing the daily P90s instead is only valid if demand on
            # consecutive days is perfectly correlated, and for an intermittent
            # series it collapses: 196 of 1,440 series have a daily P90 of
            # exactly zero on every horizon day, so the sum was zero and the P90
            # safety band was inert — the engine asked for the reserve floor and
            # nothing more, on exactly the rare-group series where the buffer
            # matters most.
            required_p90 = (
                mean + demand_dist.quantile_z() * sigma + reserve
            )

            usable_above_reserve = max(0.0, projected_available - reserve)

            probability = demand_dist.prob_demand_exceeds(
                usable_above_reserve, mean, sigma
            )

            rows.append(
                {
                    "id": new_id(),
                    "facility_id": facility_id,
                    "component_id": component_id,
                    "blood_group_id": blood_group_id,
                    "risk_date": risk_date,
                    "horizon_days": horizon_offset,
                    "on_hand_base": starting_stock,
                    "projected_available": float(projected_available),
                    "required_p50": float(required_p50),
                    "required_p90": float(required_p90),
                    "reserve_floor": float(reserve),
                    "shortage_probability": float(probability),
                    # Stock position is passed in so an empty shelf cannot be
                    # painted SAFE just because forecast demand happens to be
                    # low. See demand_dist.bucket.
                    "risk_bucket": demand_dist.bucket(
                        probability,
                        thresholds,
                        available=float(projected_available),
                        reserve_floor=float(reserve),
                    ),
                    "generated_at": generated_at,
                }
            )

            today_quantiles = series_quantiles.get(risk_date)

            if today_quantiles is not None:
                consumed += float(today_quantiles[1])

    return rows
