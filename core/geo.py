"""Distance and travel-time model (spec §8.2).

This is the single copy in the codebase. It previously existed three times, in
run_risk_rescue, run_optimizer and simulation_service, which meant a transport
limit could be enforced with one speed model and displayed with another.

MVP model: haversine x road-circuity factor, with speed bands standing in for
Pakistani road classes. Production swaps in a RoutingProvider without changing
any caller.
"""

from __future__ import annotations

import math

from core import config

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)

    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )

    return EARTH_RADIUS_KM * 2.0 * math.asin(math.sqrt(a))


def road_km(straight_line_km: float) -> float:
    factor = float(config.get("travel.circuity_factor", 1.35))
    return straight_line_km * factor


def speed_kph_for(distance_road_km: float) -> float:
    """Pick an average speed for a trip of this road distance.

    Short trips are intra-urban and slow; long trips run on motorway. A single
    flat speed either makes city trips implausibly fast or makes a
    Lahore-to-Multan run take nine hours.
    """

    bands = config.get("travel.speed_bands_kph") or []

    for band in bands:
        limit = band.get("max_road_km")

        if limit is None or distance_road_km <= float(limit):
            return float(band["kph"])

    return 45.0


def travel_minutes_from_distance(straight_line_km: float) -> int:
    """Door-to-door minutes, including loading and cold-box preparation."""

    distance = road_km(straight_line_km)
    speed = speed_kph_for(distance)
    overhead = float(config.get("travel.handling_overhead_minutes", 15))

    return int(round((distance / speed) * 60.0 + overhead))


def travel_minutes_between(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> int:
    return travel_minutes_from_distance(haversine_km(lat1, lon1, lat2, lon2))


def build_travel_matrix(facilities) -> dict[tuple[str, str], int]:
    """Symmetric all-pairs travel time in minutes, keyed by facility id."""

    matrix: dict[tuple[str, str], int] = {}

    for origin in facilities:
        for destination in facilities:
            if origin.id == destination.id:
                matrix[(origin.id, destination.id)] = 0
                continue

            if (destination.id, origin.id) in matrix:
                matrix[(origin.id, destination.id)] = matrix[
                    (destination.id, origin.id)
                ]
                continue

            matrix[(origin.id, destination.id)] = travel_minutes_between(
                float(origin.latitude),
                float(origin.longitude),
                float(destination.latitude),
                float(destination.longitude),
            )

    return matrix


def build_distance_matrix(facilities) -> dict[tuple[str, str], float]:
    """Straight-line km between every facility pair, for display and costing."""

    matrix: dict[tuple[str, str], float] = {}

    for origin in facilities:
        for destination in facilities:
            if origin.id == destination.id:
                matrix[(origin.id, destination.id)] = 0.0
                continue

            if (destination.id, origin.id) in matrix:
                matrix[(origin.id, destination.id)] = matrix[
                    (destination.id, origin.id)
                ]
                continue

            matrix[(origin.id, destination.id)] = haversine_km(
                float(origin.latitude),
                float(origin.longitude),
                float(destination.latitude),
                float(destination.longitude),
            )

    return matrix
