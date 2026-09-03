"""Per-unit expiry rescue scoring (spec §7).

96% of measured wastage in the Lahore audit was expiry, and expiry is
deterministic: the date is known at collection. Every expired unit is a decision
that was not made in time. This is the clearest ROI argument in the product, so
the scoring has to be right.

Three things were wrong before:

  * Waste probability was a hand-tuned piecewise function of queue position
    against a 14-day average of forecast demand. Spec §7.2 defines it as
    P(local_consumption < queue_position + 1), which needs an actual
    distribution — and the quantile spread is right there in the forecast.

  * Units that were not available were scored at 0.95 waste probability and
    counted in the at-risk pool. A crossmatched unit is about to be transfused;
    calling it 95% likely to be wasted inflated the headline at-risk number with
    units that were never at risk.

  * Tier assignment tested rescuability before risk, so a unit with a 5% chance
    of being wasted was labelled UNRESCUABLE purely because no needy recipient
    was reachable. 26 units were mislabelled that way. Unrescuable should mean
    "at risk and cannot be saved", not "not at risk and nobody wants it".
"""

from __future__ import annotations

from datetime import datetime, timedelta

from core import config, demand_dist

# Statuses a unit must hold to be transferable at all (spec §4.2).
TRANSFERABLE_STATUS = "AVAILABLE"
TRANSFERABLE_SCREENING = "PASSED"


def rescue_window_days(component_code: str) -> float:
    windows = config.get("expiry.rescue_window_days") or {}

    return float(windows.get(component_code, 30))


def handling_buffer_hours() -> float:
    return float(config.get("expiry.handling_buffer_hours", 12))


def tier_thresholds() -> tuple[float, float]:
    tiers = config.get("expiry.tiers") or {}

    return float(tiers.get("act_now", 0.60)), float(tiers.get("watch", 0.30))


def waste_probability(
    queue_position: int,
    window_quantiles,
) -> float:
    """P(local consumption before expiry < this unit's place in the FEFO queue).

    queue_position is 1-based: the unit with the earliest expiry at the facility
    is position 1 and is consumed first, so it is safe whenever consumption is at
    least one unit.
    """

    if not window_quantiles:
        # No forecast for this series at all. That is a data gap, not a
        # prediction of certain waste, and it must be visible as such.
        return 1.0

    mean, sigma = demand_dist.window_moments(window_quantiles)

    if mean <= 0:
        return 1.0

    return demand_dist.prob_demand_below(float(queue_position), mean, sigma)


def find_best_recipient(
    *,
    unit,
    facility_ids,
    travel_minutes,
    need_score,
    max_transport_hours: float,
    hours_left: float,
):
    """Highest-need compatible facility reachable in time.

    `need_score[(facility_id, component_id, donor_group_id)]` is the projected
    shortage-weighted demand a unit of this group could serve there.
    """

    buffer_hours = handling_buffer_hours()

    best = None
    best_score = -1.0

    for candidate_id in facility_ids:
        if candidate_id == unit["facility_id"]:
            continue

        minutes = travel_minutes.get((unit["facility_id"], candidate_id))

        if minutes is None:
            continue

        travel_hours = minutes / 60.0

        if travel_hours > max_transport_hours:
            continue

        if hours_left < travel_hours + buffer_hours:
            continue

        score = need_score.get(
            (candidate_id, unit["component_id"], unit["blood_group_id"]), 0.0
        )

        if score <= 0:
            continue

        # Prefer high need, then short travel.
        ranked = score / (minutes + 30.0)

        if ranked > best_score:
            best_score = ranked
            best = (candidate_id, int(minutes), float(score))

    return best


def classify(
    *,
    is_transferable_status: bool,
    has_cold_chain_breach: bool,
    hours_left: float,
    probability: float,
    best_recipient,
) -> tuple[str, str]:
    """Return (tier, reason).

    Risk is assessed first, then rescuability. A unit that is not at risk is
    SAFE whether or not anyone else wants it.
    """

    act_now, watch = tier_thresholds()
    buffer_hours = handling_buffer_hours()

    if not is_transferable_status:
        return (
            "NOT_TRANSFERABLE",
            "Unit is reserved, crossmatched or not screening-passed; it is "
            "allocated locally and is not available for transfer.",
        )

    if probability <= watch:
        return (
            "SAFE",
            "Projected local consumption covers this unit before it expires.",
        )

    if has_cold_chain_breach:
        return (
            "UNRESCUABLE",
            "Cold-chain breach recorded; transfer is blocked for safety.",
        )

    if hours_left < buffer_hours:
        return (
            "UNRESCUABLE",
            f"Time window closed: under {buffer_hours:.0f} hours of handling "
            "buffer remain before expiry.",
        )

    if best_recipient is None:
        return (
            "UNRESCUABLE",
            "No compatible recipient with projected demand is reachable inside "
            "this component's transport limit before expiry.",
        )

    if probability > act_now:
        return ("ACT_NOW", "")

    return ("WATCH", "")


def build_reason(tier: str, reason: str, hours_left: float, recipient_name, minutes):
    if reason:
        return reason

    if recipient_name is None:
        return "At risk of expiry; no destination selected."

    return (
        f"Expires in {hours_left:.0f} hours. Best destination is "
        f"{recipient_name}, {minutes} minutes away, which has projected demand "
        "for this group before the unit expires."
    )


def prevention_suggestions(rows, facilities_by_id, component_codes, group_codes):
    """Structural fixes, not just today's rescue (spec §7.3).

    A facility that repeatedly appears with unrescuable units of a rare group is
    not having bad luck; its standing allocation is wrong. This is the line that
    turns the tool from reactive to structural.
    """

    counts: dict[tuple, int] = {}

    for row in rows:
        if row["rescue_tier"] != "UNRESCUABLE":
            continue

        key = (row["facility_id"], row["component_id"], row["blood_group_id"])
        counts[key] = counts.get(key, 0) + 1

    suggestions = []

    for (facility_id, component_id, group_id), count in sorted(
        counts.items(), key=lambda item: item[1], reverse=True
    ):
        if count < 3:
            continue

        facility = facilities_by_id.get(facility_id)

        if facility is None:
            continue

        suggestions.append(
            {
                "facility_id": facility_id,
                "facility_name": facility.name_en,
                "component_code": component_codes.get(component_id),
                "blood_group_code": group_codes.get(group_id),
                "unrescuable_units": count,
                "suggestion": (
                    f"{facility.name_en} is holding {count} unrescuable "
                    f"{group_codes.get(group_id)} "
                    f"{component_codes.get(component_id)} units. Reduce its "
                    "standing allocation for this group and hold the buffer at "
                    "the parent RBC instead."
                ),
            }
        )

    return suggestions
