"""FEFO unit selection (spec §8.1, post-solve step).

Once the solver has fixed quantities, the specific bags have to be chosen: first
expiry, first out, among units that can still survive the journey. Never send the
freshest bag.

This step did not exist. The Transfer table had no unit column at all, so a plan
named quantities but never the physical bags — which makes acceptance criteria 6
and 7 unprovable, leaves the dispatch slip in spec §12.7 with nothing to print,
and means no one can verify after the fact that the units sent were the ones
about to expire.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from core import config


def handling_buffer_hours() -> float:
    return float(config.get("expiry.handling_buffer_hours", 12))


def eligible_units(
    units,
    *,
    now: datetime,
    travel_minutes: int,
) -> list:
    """Units that can arrive with usable shelf life left.

    A unit whose remaining life is shorter than the journey plus the handling
    buffer must not be selected, however close it is to expiry — arriving expired
    is worse than expiring in place.
    """

    required_hours = travel_minutes / 60.0 + handling_buffer_hours()
    cutoff = now + timedelta(hours=required_hours)

    return [unit for unit in units if unit["expires_at"] > cutoff]


def select_fefo(
    units,
    quantity: int,
    *,
    now: datetime,
    travel_minutes: int,
) -> tuple[list, list]:
    """Return (selected, remaining) with `quantity` units chosen FEFO."""

    if quantity <= 0:
        return [], list(units)

    eligible = eligible_units(units, now=now, travel_minutes=travel_minutes)
    eligible.sort(key=lambda unit: unit["expires_at"])

    selected = eligible[:quantity]
    selected_ids = {unit["id"] for unit in selected}

    remaining = [unit for unit in units if unit["id"] not in selected_ids]

    return selected, remaining


class UnitPool:
    """Tracks which physical bags are still unclaimed as a plan is assembled.

    Without this, two transfers in the same plan can both be assigned the same
    bag, and the plan is not executable.
    """

    def __init__(self, units_by_series: dict):
        self._pool = {
            key: sorted(units, key=lambda unit: unit["expires_at"])
            for key, units in units_by_series.items()
        }

    def available(self, key) -> int:
        return len(self._pool.get(key, []))

    def take(self, key, quantity: int, *, now: datetime, travel_minutes: int):
        units = self._pool.get(key, [])

        selected, remaining = select_fefo(
            units, quantity, now=now, travel_minutes=travel_minutes
        )

        self._pool[key] = remaining

        return selected
