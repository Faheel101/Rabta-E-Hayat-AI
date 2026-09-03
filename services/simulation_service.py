from __future__ import annotations

import math
import time as time_module
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import func, select

from config.settings import DEMO_DATE
from core import config, geo, policy
from db.models import (
    BloodGroup,
    BloodUnit,
    Compatibility,
    Component,
    Facility,
    Forecast,
    Organization,
    SimulationRun,
    new_id,
)
from services.audit import Actor, ServiceError, audited, require, snapshot
from app.auth import Permission


DEMO_DATETIME = datetime.combine(DEMO_DATE, time(8, 0, 0), tzinfo=timezone.utc)

DEFAULT_SEED = int(config.get("simulation.default_seed", 42))
DEFAULT_ITERATIONS = int(config.get("simulation.default_iterations", 1000))
EMERGENCY_RESERVE_FACTOR = float(
    config.get("simulation.emergency_reserve_release", 0.50)
)
ROUTINE_COMMITMENT_QUANTILE = "p90"
FALLBACK_FREE_SUPPLY_RATIO = 0.30

# Share of a group's free stock that can realistically be mobilised into a surge.
# Spec §9.3 requires every one of these to be configuration, editable in Admin and
# validated by a transfusion medicine specialist — never hardcoded. They also
# compound with the reserve release and the committed-demand subtraction above, so
# they must be read as a third discount and reviewed as such.
GROUP_SURGE_AVAILABILITY = dict(
    config.get("simulation.group_surge_availability")
    or {"O-": 0.15, "B-": 0.25, "A-": 0.30, "AB-": 0.15, "default": 0.70}
)

TRAUMA_EVENTS = {
    "RTA_MASS",
    "BUS_ACCIDENT",
    "EARTHQUAKE",
    "BUILDING_COLLAPSE",
    "BLAST",
    "INDUSTRIAL_FIRE",
}

P_TRANSFUSE = dict(config.require("simulation.p_transfusion"))
MEAN_PRBC = dict(config.require("simulation.mean_prbc_units"))
DEFAULT_SEVERITY_MIX = dict(config.require("simulation.default_severity_mix"))

MASSIVE_TRANSFUSION_PROBABILITY = float(
    config.get("simulation.massive_transfusion_probability", 0.40)
)
MTP_FFP_RATIO = float(config.get("simulation.mtp_ffp_ratio", 1.0))
MTP_RBC_PER_PLATELET_DOSE = float(
    config.get("simulation.mtp_rbc_per_platelet_dose", 6)
)

_donor = config.get("simulation.donor") or {}
DONOR_YIELD_PER_DONOR = float(_donor.get("yield_per_donor", 1.0))
DONOR_SHOW_UP_RATE = float(_donor.get("show_up_rate", 0.55))
DONOR_ELIGIBILITY_RATE = float(_donor.get("eligibility_rate", 0.85))

MAX_EMERGENCY_TRANSFERS = int(
    config.get("simulation.max_emergency_transfers", 20)
)
FACILITY_SHARE_CAP = float(config.get("simulation.facility_share_cap", 0.35))
SURGE_CAPACITY_PER_100_BEDS = float(
    config.get("simulation.surge_capacity_per_100_beds", 18)
)
RBC_SURGE_CAPACITY = int(config.get("simulation.rbc_surge_capacity", 80))
ROAD_BLOCK_MULTIPLIER = float(
    config.get("simulation.road_block_travel_multiplier", 1.75)
)

EVENT_TYPES = {
    "RTA_MASS",
    "BUS_ACCIDENT",
    "EARTHQUAKE",
    "FLOOD",
    "BUILDING_COLLAPSE",
    "BLAST",
    "INDUSTRIAL_FIRE",
    "HEATWAVE",
    "DENGUE_OUTBREAK",
    "CUSTOM",
}
ONSET_PROFILES = {"INSTANT", "RAMP_6H", "MULTI_DAY"}
SIMULATION_FIELDS = (
    "organization_id",
    "created_by",
    "mode",
    "parent_run_id",
    "name",
    "event_type",
    "seed",
    "iterations",
    "status",
    "duration_ms",
)


def scenario_presets() -> dict:
    """Configured demonstration presets, copied so callers cannot mutate config."""

    presets = {}
    for code, values in (config.get("simulation.presets") or {}).items():
        preset = dict(values or {})
        preset.setdefault("event_type", str(code))
        presets[str(code)] = preset
    return presets


def validate_scenario(scenario: dict) -> dict:
    """Return a bounded canonical scenario or refuse invalid clinical inputs."""

    values = dict(scenario or {})
    event_type = str(values.get("event_type", "CUSTOM")).upper()
    if event_type not in EVENT_TYPES:
        raise ServiceError("EVENT_TYPE_INVALID", "Choose a supported event type.", field="event_type")
    onset = str(values.get("onset_profile", "RAMP_6H")).upper()
    if onset not in ONSET_PROFILES:
        raise ServiceError("ONSET_INVALID", "Choose a supported onset profile.", field="onset_profile")

    try:
        casualties = int(values.get("casualties", 60))
        iterations = int(values.get("iterations", DEFAULT_ITERATIONS))
        duration = int(values.get("duration_hours", 12))
        seed = int(values.get("seed", DEFAULT_SEED))
        latitude = float(values.get("epicenter_lat", 31.5497))
        longitude = float(values.get("epicenter_lon", 74.3436))
    except (TypeError, ValueError) as exc:
        raise ServiceError("SCENARIO_NUMBER_INVALID", "Scenario numbers are invalid.") from exc

    if not 1 <= casualties <= int(config.get("simulation.max_casualties", 5000)):
        raise ServiceError("CASUALTIES_INVALID", "Casualties must be between 1 and 5,000.", field="casualties")
    minimum_iterations = int(config.get("simulation.min_iterations", 100))
    maximum_iterations = int(config.get("simulation.max_iterations", 5000))
    if not minimum_iterations <= iterations <= maximum_iterations:
        raise ServiceError("ITERATIONS_INVALID", f"Iterations must be between {minimum_iterations} and {maximum_iterations}.", field="iterations")
    if not 1 <= duration <= 168:
        raise ServiceError("DURATION_INVALID", "Duration must be between 1 and 168 hours.", field="duration_hours")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ServiceError("EPICENTRE_INVALID", "Epicentre coordinates are invalid.")

    mix = values.get("severity_mix") or DEFAULT_SEVERITY_MIX
    normalized_mix = {}
    try:
        for severity in ("MINOR", "MODERATE", "SEVERE", "CRITICAL"):
            normalized_mix[severity] = max(0.0, float(mix.get(severity, 0.0)))
    except (TypeError, ValueError) as exc:
        raise ServiceError("SEVERITY_MIX_INVALID", "Severity percentages are invalid.") from exc
    total = sum(normalized_mix.values())
    if total <= 0:
        raise ServiceError("SEVERITY_MIX_INVALID", "Severity mix must have a positive total.")
    normalized_mix = {key: round(value / total, 6) for key, value in normalized_mix.items()}

    for field in ("facilities_degraded_pct", "degraded_capacity_loss_pct"):
        try:
            number = float(values.get(field, 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ServiceError("INFRASTRUCTURE_INVALID", "Infrastructure impact is invalid.", field=field) from exc
        if not 0 <= number <= 100:
            raise ServiceError("INFRASTRUCTURE_INVALID", "Infrastructure percentages must be between 0 and 100.", field=field)
        values[field] = number

    release = float(values.get("emergency_reserve_release_pct", 50) or 0)
    if not 0 <= release <= 100:
        raise ServiceError("RESERVE_RELEASE_INVALID", "Emergency reserve release must be between 0 and 100%.", field="emergency_reserve_release_pct")

    def as_bool(value) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    mode = str(values.get("mode", "PREPAREDNESS")).upper()
    if mode != "PREPAREDNESS":
        raise ServiceError(
            "SIMULATION_MODE_INVALID",
            "A scenario run must remain in preparedness mode until it is explicitly declared live.",
            field="mode",
        )

    return {
        **values,
        "name": str(values.get("name") or event_type.replace("_", " ").title())[:255],
        "event_type": event_type,
        "onset_profile": onset,
        "casualties": casualties,
        "iterations": iterations,
        "duration_hours": duration,
        "seed": seed,
        "epicenter_lat": latitude,
        "epicenter_lon": longitude,
        "severity_mix": normalized_mix,
        "impact_radius_km": max(1.0, min(500.0, float(values.get("impact_radius_km", 80) or 80))),
        "roads_blocked": as_bool(values.get("roads_blocked", False)),
        "release_emergency_reserves": as_bool(
            values.get("release_emergency_reserves", True)
        ),
        "emergency_reserve_release_pct": release,
        "mode": mode,
    }


# Geography and reserve policy come from the shared kernel. This module used to
# carry its own copy of both: a flat-45km/h travel model that disagreed with the
# one the optimizer enforced transport limits with, and a reserve lookup that
# read `min_reserve_policy_json[component]["default"]`. Once the policy became
# per-blood-group, that lookup silently returned zero for every facility, so the
# simulator believed the network had no clinical reserve at all and released the
# entire inventory into every surge.
haversine_km = geo.haversine_km
travel_minutes_from_distance = geo.travel_minutes_from_distance
build_facility_travel_matrix = geo.build_travel_matrix


def get_reserve_total(facility: Facility, component_code: str) -> float:
    return policy.component_reserve_total(facility, component_code)


def sample_count(mean: float, rng: np.random.Generator) -> int:
    if mean <= 0:
        return 0

    if mean < 2.5:
        return int(rng.poisson(mean))

    p = 5.0 / (5.0 + mean)
    return int(rng.negative_binomial(5, p))


def load_reference(session):
    facilities = session.scalars(
        select(Facility).where(Facility.is_active.is_(True))
    ).all()

    components = session.scalars(select(Component)).all()
    groups = session.scalars(select(BloodGroup)).all()

    component_id_by_code = {c.code: c.id for c in components}
    component_code_map = {c.id: c.code for c in components}
    component_by_id = {c.id: c for c in components}

    group_code_map = {g.id: g.code for g in groups}

    total_pct = sum(float(g.population_pct_pk or 0.0) for g in groups)
    if total_pct <= 0:
        total_pct = 100.0

    group_ids = [g.id for g in groups]
    group_probs = np.array(
        [float(g.population_pct_pk or 0.0) / total_pct for g in groups],
        dtype=float,
    )

    return (
        facilities,
        components,
        groups,
        component_id_by_code,
        component_code_map,
        component_by_id,
        group_code_map,
        group_ids,
        group_probs,
    )


def load_usable_inventory(session):
    units_df = pd.read_sql(
        select(
            BloodUnit.facility_id,
            BloodUnit.component_id,
            BloodUnit.blood_group_id,
            BloodUnit.status,
            BloodUnit.screening_status,
            BloodUnit.expires_at,
        ),
        session.bind,
    )

    units_df["expires_at"] = pd.to_datetime(units_df["expires_at"], utc=True)

    usable = units_df[
        (units_df["status"] == "AVAILABLE")
        & (units_df["screening_status"] == "PASSED")
        & (units_df["expires_at"] > DEMO_DATETIME)
    ].copy()

    available_by_component_donor = (
        usable.groupby(["component_id", "blood_group_id"])
        .size()
        .to_dict()
    )

    facility_supply = (
        usable.groupby(["facility_id", "component_id", "blood_group_id"])
        .size()
        .to_dict()
    )

    return available_by_component_donor, facility_supply

def build_free_supply(
    session,
    facilities,
    facility_supply,
    component_code_map,
    groups,
    duration_hours,
    *,
    reserve_hold_factor: float = 1.0,
    degraded_facilities: set[str] | None = None,
    degraded_capacity_loss: float = 0.0,
):
    """Stock available to the surge after routine demand and protected reserve.

    ``reserve_hold_factor`` is 1.0 for the current position. An intervention may
    deliberately lower it, but never bypasses routine committed demand. Facility
    degradation also removes the inaccessible share of stock from the plan.
    """

    facility_by_id = {facility.id: facility for facility in facilities}
    degraded_facilities = degraded_facilities or set()
    reserve_hold_factor = min(1.0, max(0.0, float(reserve_hold_factor)))
    degraded_capacity_loss = min(1.0, max(0.0, float(degraded_capacity_loss)))

    total_pct = sum(float(group.population_pct_pk or 0.0) for group in groups)

    if total_pct <= 0:
        total_pct = 100.0

    group_share = {
        group.id: float(group.population_pct_pk or 0.0) / total_pct
        for group in groups
    }

    group_code_map = {group.id: group.code for group in groups}

    window_days = max(1, math.ceil(duration_hours / 24.0))
    end_date = DEMO_DATE + timedelta(days=window_days)

    stmt = select(
        Forecast.facility_id,
        Forecast.component_id,
        Forecast.blood_group_id,
        func.sum(Forecast.p50).label("p50"),
        func.sum(Forecast.p90).label("p90"),
    ).where(
        Forecast.target_date >= DEMO_DATE,
        Forecast.target_date < end_date,
    ).group_by(
        Forecast.facility_id,
        Forecast.component_id,
        Forecast.blood_group_id,
    )

    forecast_df = pd.read_sql(stmt, session.bind)

    scale = duration_hours / (24.0 * window_days)

    committed_map = {}

    if not forecast_df.empty:
        for row in forecast_df.itertuples():
            if ROUTINE_COMMITMENT_QUANTILE == "p90":
                committed_units = float(row.p90)
            else:
                committed_units = float(row.p50)

            committed_map[
                (row.facility_id, row.component_id, row.blood_group_id)
            ] = committed_units * scale

    free_facility_supply = {}

    for key, on_hand in facility_supply.items():
        facility_id, component_id, blood_group_id = key

        facility = facility_by_id.get(facility_id)

        if facility is None:
            continue

        component_code = component_code_map.get(component_id, "")

        reserve_total = get_reserve_total(facility, component_code)

        reserve_allocation = (
            reserve_total
            * group_share.get(blood_group_id, 0.0)
            * reserve_hold_factor
        )

        committed_demand = committed_map.get(key, 0.0)

        if committed_map:
            free_units = int(
                math.floor(
                    float(on_hand)
                    - committed_demand
                    - reserve_allocation
                )
            )
        else:
            free_units = int(
                math.floor(
                    float(on_hand) * FALLBACK_FREE_SUPPLY_RATIO
                )
            )

        if free_units > 0:
            if facility_id in degraded_facilities:
                free_units = int(
                    math.floor(free_units * (1.0 - degraded_capacity_loss))
                )

            group_code = group_code_map.get(blood_group_id)

            surge_factor = GROUP_SURGE_AVAILABILITY.get(
                group_code,
                GROUP_SURGE_AVAILABILITY.get("default", 1.0),
            )

            free_units = int(math.floor(free_units * surge_factor))

            if free_units > 0:
                free_facility_supply[key] = free_units

    available_by_component_donor = defaultdict(int)

    for (facility_id, component_id, blood_group_id), count in free_facility_supply.items():
        available_by_component_donor[(component_id, blood_group_id)] += count

    return dict(available_by_component_donor), free_facility_supply


def load_compatibility(session):
    compat_df = pd.read_sql(
        select(
            Compatibility.component_id,
            Compatibility.recipient_group_id,
            Compatibility.donor_group_id,
        ).where(Compatibility.is_compatible.is_(True)),
        session.bind,
    )

    recipient_to_donors = defaultdict(list)

    for row in compat_df.itertuples():
        recipient_to_donors[
            (row.component_id, row.recipient_group_id)
        ].append(row.donor_group_id)

    return recipient_to_donors


def normalize_severity_mix(scenario_mix: dict | None) -> tuple[list[str], np.ndarray]:
    mix = scenario_mix or DEFAULT_SEVERITY_MIX

    normalized = {
        str(key).upper(): max(0.0, float(value))
        for key, value in mix.items()
    }

    if sum(normalized.values()) <= 0:
        normalized = DEFAULT_SEVERITY_MIX.copy()

    names = list(normalized.keys())
    probs = np.array([normalized[name] for name in names], dtype=float)
    probs = probs / probs.sum()

    return names, probs


def build_facility_weights(
    facilities,
    epicenter_lat: float,
    epicenter_lon: float,
    event_type: str,
    *,
    degraded_facilities: set[str] | None = None,
    degraded_capacity_loss: float = 0.0,
):
    degraded_facilities = degraded_facilities or set()
    weights = []

    for facility in facilities:
        distance_km = haversine_km(
            epicenter_lat,
            epicenter_lon,
            float(facility.latitude),
            float(facility.longitude),
        )

        travel_minutes = travel_minutes_from_distance(distance_km)

        if facility.facility_type == "RBC":
            base_capacity = max(float(facility.bed_count or 0), 80.0)
        else:
            base_capacity = max(float(facility.bed_count or 0), 30.0)

        tau_config = config.get("simulation.gravity_tau_minutes") or {}

        if event_type in TRAUMA_EVENTS:
            capability = 2.5 if facility.has_trauma_centre else 1.0
            tau = float(tau_config.get("trauma", 30.0))
        else:
            capability = 1.0
            tau = float(tau_config.get("other", 45.0))

        weight = capability * base_capacity * math.exp(-travel_minutes / tau)

        if facility.id in degraded_facilities:
            weight *= 1.0 - min(1.0, max(0.0, degraded_capacity_loss))

        weights.append(max(0.0001, weight))

    weights = np.array(weights, dtype=float)

    if weights.sum() <= 0:
        weights = np.ones(len(facilities), dtype=float)

    weights = weights / weights.sum()

    # Soft surge cap so one facility does not absorb everything.
    cap = FACILITY_SHARE_CAP
    if np.any(weights > cap):
        weights = np.minimum(weights, cap)
        weights = weights / weights.sum()

    return weights


def infrastructure_impact(facilities, scenario: dict) -> dict:
    """Select affected and degraded facilities reproducibly from the scenario.

    A separate random stream keeps infrastructure selection stable when the
    clinical simulation changes its internal sampling order.
    """

    radius = float(scenario.get("impact_radius_km", 80.0))
    affected = []
    distances = {}
    for facility in facilities:
        distance = haversine_km(
            float(scenario["epicenter_lat"]),
            float(scenario["epicenter_lon"]),
            float(facility.latitude),
            float(facility.longitude),
        )
        distances[facility.id] = round(distance, 1)
        if distance <= radius:
            affected.append(facility.id)

    degraded_pct = float(scenario.get("facilities_degraded_pct", 0.0))
    degraded_count = min(
        len(affected),
        int(math.ceil(len(affected) * degraded_pct / 100.0)),
    )
    infrastructure_rng = np.random.default_rng(int(scenario["seed"]) ^ 0x5EED5EED)
    degraded = []
    if degraded_count:
        degraded = sorted(
            str(value)
            for value in infrastructure_rng.choice(
                np.array(affected, dtype=object),
                size=degraded_count,
                replace=False,
            ).tolist()
        )

    return {
        "impact_radius_km": radius,
        "affected_facility_ids": sorted(affected),
        "degraded_facility_ids": degraded,
        "degraded_capacity_loss_pct": float(
            scenario.get("degraded_capacity_loss_pct", 0.0)
        ),
        "roads_blocked": bool(scenario.get("roads_blocked")),
        "facility_distances_km": distances,
    }


def facility_surge_capacities(
    facilities,
    *,
    degraded_facilities: set[str],
    degraded_capacity_loss: float,
) -> np.ndarray:
    capacities = []
    for facility in facilities:
        if facility.facility_type == "RBC":
            capacity = RBC_SURGE_CAPACITY
        else:
            capacity = max(
                5,
                int(
                    math.ceil(
                        max(1, int(facility.bed_count or 0))
                        / 100.0
                        * SURGE_CAPACITY_PER_100_BEDS
                    )
                ),
            )
        if facility.id in degraded_facilities:
            capacity = max(1, int(math.floor(capacity * (1.0 - degraded_capacity_loss))))
        capacities.append(capacity)
    return np.array(capacities, dtype=np.int32)


def sample_facility_assignments(
    rng: np.random.Generator,
    casualties: int,
    weights: np.ndarray,
    capacities: np.ndarray,
) -> tuple[np.ndarray, int]:
    """Gravity allocation with a hard facility surge ceiling.

    Casualties above total network surge capacity remain an explicit unplaced
    count rather than being silently loaded into a facility that cannot receive
    them. The sampled casualty clinical profiles remain network requirements;
    unplaced cases are assigned to the nearest-capable regional centre only for
    blood-demand accounting and are separately reported as a capacity warning.
    """

    target = min(int(casualties), int(capacities.sum()))
    if target <= 0:
        return np.zeros(0, dtype=np.int32), int(casualties)

    counts = np.zeros(len(weights), dtype=np.int32)
    remaining = target
    available = capacities.astype(np.int32).copy()
    while remaining > 0 and int(available.sum()) > 0:
        eligible = available > 0
        probabilities = weights * eligible
        probabilities = probabilities / probabilities.sum()
        draw = rng.multinomial(remaining, probabilities)
        accepted = np.minimum(draw, available)
        assigned = int(accepted.sum())
        counts += accepted
        available -= accepted
        remaining -= assigned

    assignments = np.repeat(np.arange(len(weights), dtype=np.int32), counts)
    rng.shuffle(assignments)
    return assignments, int(casualties) - len(assignments)


def onset_weights(onset_profile: str, duration_hours: int):
    duration_hours = max(1, int(duration_hours))

    if onset_profile == "INSTANT":
        weights = np.zeros(duration_hours + 1)
        weights[0] = 1.0

    elif onset_profile == "RAMP_6H":
        ramp_hours = min(6, duration_hours)
        weights = np.zeros(duration_hours + 1)
        weights[: ramp_hours + 1] = np.arange(ramp_hours + 1) + 1.0

    else:
        weights = np.ones(duration_hours + 1)

    if weights.sum() <= 0:
        weights = np.ones(duration_hours + 1)

    return weights / weights.sum()


def run_simulation(
    session,
    scenario: dict,
    save: bool = True,
    *,
    actor: Actor | None = None,
    parent_run_id: str | None = None,
    comparison_base: dict | None = None,
) -> dict:
    started = time_module.perf_counter()
    scenario = validate_scenario(scenario)
    if actor is not None:
        require(actor, Permission.RUN_SIMULATION, "run emergency simulations")
    seed = scenario["seed"]
    iterations = scenario["iterations"]
    casualties = scenario["casualties"]
    event_type = scenario["event_type"]
    onset_profile = scenario["onset_profile"]
    duration_hours = scenario["duration_hours"]

    epicenter_lat = scenario["epicenter_lat"]
    epicenter_lon = scenario["epicenter_lon"]

    rng = np.random.default_rng(seed)

    (
        facilities,
        components,
        groups,
        component_id_by_code,
        component_code_map,
        component_by_id,
        group_code_map,
        group_ids,
        group_probs,
    ) = load_reference(session)

    if not facilities:
        raise ServiceError("NO_FACILITIES", "No active facilities are available for simulation.")

    infrastructure = infrastructure_impact(facilities, scenario)
    degraded_facilities = set(infrastructure["degraded_facility_ids"])
    degraded_capacity_loss = infrastructure["degraded_capacity_loss_pct"] / 100.0

    _, raw_facility_supply = load_usable_inventory(session)

    _, current_facility_supply = build_free_supply(
        session,
        facilities,
        raw_facility_supply,
        component_code_map,
        groups,
        duration_hours,
        reserve_hold_factor=1.0,
        degraded_facilities=degraded_facilities,
        degraded_capacity_loss=degraded_capacity_loss,
    )
    reserve_hold_factor = 1.0
    if scenario["release_emergency_reserves"]:
        reserve_hold_factor = 1.0 - scenario["emergency_reserve_release_pct"] / 100.0
    available_by_component_donor, facility_supply = build_free_supply(
        session,
        facilities,
        raw_facility_supply,
        component_code_map,
        groups,
        duration_hours,
        reserve_hold_factor=reserve_hold_factor,
        degraded_facilities=degraded_facilities,
        degraded_capacity_loss=degraded_capacity_loss,
    )
    recipient_to_donors = load_compatibility(session)
    facility_travel = build_facility_travel_matrix(facilities)

    severity_names, severity_probs = normalize_severity_mix(
        scenario.get("severity_mix")
    )

    facility_ids = [facility.id for facility in facilities]
    facility_name_map = {facility.id: facility.name_en for facility in facilities}
    facility_by_id = {facility.id: facility for facility in facilities}
    organization_opt_in = {
        item.id: bool(item.network_opt_in)
        for item in session.scalars(select(Organization)).all()
    }

    def sharing_allowed(source_id: str, destination_id: str) -> bool:
        source = facility_by_id[source_id]
        destination = facility_by_id[destination_id]
        if source.organization_id == destination.organization_id:
            return True
        return bool(
            source.shares_inventory
            and destination.shares_inventory
            and organization_opt_in.get(source.organization_id, False)
            and organization_opt_in.get(destination.organization_id, False)
        )

    affected_facilities = set(infrastructure["affected_facility_ids"])

    def route_travel_minutes(source_id: str, destination_id: str) -> int:
        travel = float(facility_travel.get((source_id, destination_id), 9999))
        if scenario["roads_blocked"] and (
            source_id in affected_facilities or destination_id in affected_facilities
        ):
            travel *= ROAD_BLOCK_MULTIPLIER
        return int(math.ceil(travel))

    facility_weights = build_facility_weights(
        facilities,
        epicenter_lat,
        epicenter_lon,
        event_type,
        degraded_facilities=degraded_facilities,
        degraded_capacity_loss=degraded_capacity_loss,
    )
    surge_capacities = facility_surge_capacities(
        facilities,
        degraded_facilities=degraded_facilities,
        degraded_capacity_loss=degraded_capacity_loss,
    )

    requirement_component_codes = ["PRBC", "FFP", "PLT_RD"]
    requirement_component_ids = [
        component_id_by_code[code]
        for code in requirement_component_codes
        if code in component_id_by_code
    ]

    series_keys = []
    for facility_id in facility_ids:
        for component_id in requirement_component_ids:
            for group_id in group_ids:
                series_keys.append((facility_id, component_id, group_id))

    series_index = {key: idx for idx, key in enumerate(series_keys)}

    cg_keys = []
    for component_id in requirement_component_ids:
        for group_id in group_ids:
            cg_keys.append((component_id, group_id))

    cg_index = {key: idx for idx, key in enumerate(cg_keys)}

    facility_results = np.zeros(
        (iterations, len(series_keys)),
        dtype=np.float32,
    )

    cg_results = np.zeros(
        (iterations, len(cg_keys)),
        dtype=np.float32,
    )
    unplaced_cg_results = np.zeros(
        (iterations, len(cg_keys)),
        dtype=np.float32,
    )

    prbc_component_id = component_id_by_code.get("PRBC")
    ffp_component_id = component_id_by_code.get("FFP")
    plt_component_id = component_id_by_code.get("PLT_RD")

    unplaced_casualties = 0
    for iteration in range(iterations):
        facility_idxs, iteration_unplaced = sample_facility_assignments(
            rng,
            casualties,
            facility_weights,
            surge_capacities,
        )
        unplaced_casualties = max(unplaced_casualties, iteration_unplaced)
        if iteration_unplaced:
            facility_idxs = np.concatenate(
                [facility_idxs, np.full(iteration_unplaced, -1, dtype=np.int32)]
            )
            rng.shuffle(facility_idxs)

        severity_idxs = rng.choice(
            len(severity_names),
            size=casualties,
            p=severity_probs,
        )

        group_idxs = rng.choice(
            len(group_ids),
            size=casualties,
            p=group_probs,
        )

        for casualty_idx in range(casualties):
            severity = severity_names[int(severity_idxs[casualty_idx])]

            if rng.random() >= P_TRANSFUSE.get(severity, 0.0):
                continue

            mean_prbc = MEAN_PRBC.get(severity, 0.0)
            prbc_units = sample_count(mean_prbc, rng)

            if prbc_units <= 0:
                continue

            massive = (
                severity == "CRITICAL"
                and rng.random() < MASSIVE_TRANSFUSION_PROBABILITY
            )

            if massive:
                ffp_units = int(round(prbc_units * MTP_FFP_RATIO))
                plt_units = math.ceil(prbc_units / MTP_RBC_PER_PLATELET_DOSE)
            else:
                ffp_units = int(rng.poisson(max(0.0, prbc_units * 0.4)))
                plt_units = int(rng.poisson(max(0.2, prbc_units / 6.0)))

            facility_index = int(facility_idxs[casualty_idx])
            facility_id = facility_ids[facility_index] if facility_index >= 0 else None
            group_id = group_ids[int(group_idxs[casualty_idx])]

            if prbc_component_id is not None:
                cg_key = (prbc_component_id, group_id)

                if facility_id is not None:
                    series_key = (facility_id, prbc_component_id, group_id)
                    facility_results[iteration, series_index[series_key]] += prbc_units
                else:
                    unplaced_cg_results[iteration, cg_index[cg_key]] += prbc_units
                cg_results[iteration, cg_index[cg_key]] += prbc_units

            if ffp_component_id is not None and ffp_units > 0:
                cg_key = (ffp_component_id, group_id)

                if facility_id is not None:
                    series_key = (facility_id, ffp_component_id, group_id)
                    facility_results[iteration, series_index[series_key]] += ffp_units
                else:
                    unplaced_cg_results[iteration, cg_index[cg_key]] += ffp_units
                cg_results[iteration, cg_index[cg_key]] += ffp_units

            if plt_component_id is not None and plt_units > 0:
                cg_key = (plt_component_id, group_id)

                if facility_id is not None:
                    series_key = (facility_id, plt_component_id, group_id)
                    facility_results[iteration, series_index[series_key]] += plt_units
                else:
                    unplaced_cg_results[iteration, cg_index[cg_key]] += plt_units
                cg_results[iteration, cg_index[cg_key]] += plt_units

    iteration_totals = cg_results.sum(axis=1)

    units_required_p50 = int(np.ceil(np.percentile(iteration_totals, 50)))
    units_required_p95 = int(np.ceil(np.percentile(iteration_totals, 95)))

    series_p50 = np.percentile(facility_results, 50, axis=0)
    series_p95 = np.percentile(facility_results, 95, axis=0)

    cg_p50 = np.percentile(cg_results, 50, axis=0)
    cg_p95 = np.percentile(cg_results, 95, axis=0)
    unplaced_cg_p95 = np.percentile(unplaced_cg_results, 95, axis=0)

    plt_aph_component_id = component_id_by_code.get("PLT_APH")
    wb_component_id = component_id_by_code.get("WB")

    def available_for_donor(requirement_component_id: int, donor_group_id: int) -> int:
        total = available_by_component_donor.get(
            (requirement_component_id, donor_group_id),
            0,
        )

        requirement_code = component_code_map.get(requirement_component_id)

        if requirement_code == "PLT_RD" and plt_aph_component_id is not None:
            total += available_by_component_donor.get(
                (plt_aph_component_id, donor_group_id),
                0,
            )

        if requirement_code == "PRBC" and wb_component_id is not None:
            total += available_by_component_donor.get(
                (wb_component_id, donor_group_id),
                0,
            )

        return int(total)

    requirement_by_group_component = []
    network_can_supply = 0
    gap_by_group = defaultdict(int)
    planning_required_p50 = 0
    planning_required_p95 = 0

    for idx, (component_id, group_id) in enumerate(cg_keys):
        required_p50 = int(np.ceil(float(cg_p50[idx])))
        required_p95 = int(np.ceil(float(cg_p95[idx])))

        if required_p95 <= 0:
            continue

        compatible_donors = recipient_to_donors.get((component_id, group_id), [])

        compatible_available = 0

        for donor_group_id in compatible_donors:
            compatible_available += available_for_donor(component_id, donor_group_id)

        covered = min(required_p95, compatible_available)
        gap = max(0, required_p95 - covered)

        network_can_supply += covered
        gap_by_group[group_id] += gap
        planning_required_p50 += required_p50
        planning_required_p95 += required_p95

        requirement_by_group_component.append(
            {
                "component_id": int(component_id),
                "component_code": component_code_map.get(component_id),
                "blood_group_id": int(group_id),
                "blood_group_code": group_code_map.get(group_id),
                "required_p50": required_p50,
                "required_p95": required_p95,
                "covered": int(covered),
                "gap": int(gap),
            }
        )

    monte_carlo_total_p50 = units_required_p50
    monte_carlo_total_p95 = units_required_p95

    # Summing the marginal P95 of each component-group cell is not the P95 of the
    # total — quantiles are not additive, and the sum is systematically higher
    # (978 against a true 702 on the reference scenario, a 39% overstatement).
    # It is a legitimate *planning* figure, because a controller must cover each
    # group separately and cannot net a surplus of B+ against a shortfall of O-.
    # So both are reported, each under its own name, and neither is called the
    # other.
    planning_requirement_p50 = planning_required_p50
    planning_requirement_p95 = planning_required_p95

    units_required_p50 = monte_carlo_total_p50
    units_required_p95 = monte_carlo_total_p95

    requirement_by_group_component.sort(
        key=lambda item: item["required_p95"],
        reverse=True,
    )

    def local_available_for_recipient(
        facility_id: str,
        requirement_component_id: int,
        recipient_group_id: int,
        supply_map: dict,
    ) -> int:
        compatible_donors = recipient_to_donors.get(
            (requirement_component_id, recipient_group_id),
            [],
        )

        total = 0

        requirement_code = component_code_map.get(requirement_component_id)
        local_component_ids = [requirement_component_id]

        if requirement_code == "PLT_RD" and plt_aph_component_id is not None:
            local_component_ids.append(plt_aph_component_id)

        if requirement_code == "PRBC" and wb_component_id is not None:
            local_component_ids.append(wb_component_id)

        for donor_group_id in compatible_donors:
            for local_component_id in local_component_ids:
                total += supply_map.get(
                    (facility_id, local_component_id, donor_group_id),
                    0,
                )

        return int(total)

    facility_requirements = []

    for idx, key in enumerate(series_keys):
        required_p95 = int(np.ceil(float(series_p95[idx])))

        if required_p95 <= 0:
            continue

        facility_id, component_id, group_id = key

        current_local_available = local_available_for_recipient(
            facility_id,
            component_id,
            group_id,
            current_facility_supply,
        )
        intervention_local_available = local_available_for_recipient(
            facility_id,
            component_id,
            group_id,
            facility_supply,
        )

        gap = max(0, required_p95 - current_local_available)
        intervention_gap = max(0, required_p95 - intervention_local_available)

        facility_requirements.append(
            {
                "facility_id": facility_id,
                "facility_name": facility_name_map.get(facility_id),
                "component_id": int(component_id),
                "component_code": component_code_map.get(component_id),
                "blood_group_id": int(group_id),
                "blood_group_code": group_code_map.get(group_id),
                "required_p50": int(np.ceil(float(series_p50[idx]))),
                "required_p95": required_p95,
                "local_available": int(current_local_available),
                "available_after_reserve_release": int(intervention_local_available),
                "gap": int(gap),
                "gap_after_reserve_release": int(intervention_gap),
            }
        )

    facility_requirements.sort(
        key=lambda item: item["required_p95"],
        reverse=True,
    )

    # Coverage before any action, measured the same way as coverage after the
    # plan: per facility, against local stock.
    #
    # Pooling the whole network's inventory against the requirement instead —
    # which an earlier version did — reports full coverage even when every unit
    # sits in the wrong building, so the "before" figure came out at 100% and the
    # correctly-computed "after" figure at 86.8%, making the emergency plan look
    # like it made things worse. The two numbers have to be measured on the same
    # basis or the before/after comparison is meaningless.
    placed_facility_requirement_total = sum(
        item["required_p95"] for item in facility_requirements
    )
    unplaced_requirement_p95 = sum(
        int(math.ceil(float(value))) for value in unplaced_cg_p95
    )
    facility_requirement_total = (
        placed_facility_requirement_total + unplaced_requirement_p95
    )
    facility_covered_before = sum(
        item["required_p95"] - item["gap"] for item in facility_requirements
    )

    remaining_supply = dict(facility_supply)
    remaining_gap = {
        (
            item["facility_id"],
            item["component_id"],
            item["blood_group_id"],
        ): item["gap_after_reserve_release"]
        for item in facility_requirements
        if item["gap"] > 0
    }

    emergency_transfers = []
    max_emergency_transfers = MAX_EMERGENCY_TRANSFERS

    sorted_gap_items = sorted(
        remaining_gap.items(),
        key=lambda item: item[1],
        reverse=True,
    )

    for series_key, gap in sorted_gap_items:
        if gap <= 0 or len(emergency_transfers) >= max_emergency_transfers:
            continue

        dest_facility_id, requirement_component_id, recipient_group_id = series_key

        requirement_code = component_code_map.get(requirement_component_id)

        source_component_ids = [requirement_component_id]

        if requirement_code == "PLT_RD" and plt_aph_component_id is not None:
            source_component_ids.append(plt_aph_component_id)

        if requirement_code == "PRBC" and wb_component_id is not None:
            source_component_ids.append(wb_component_id)

        compatible_donors = recipient_to_donors.get(
            (requirement_component_id, recipient_group_id),
            [],
        )

        for donor_group_id in compatible_donors:
            if gap <= 0 or len(emergency_transfers) >= max_emergency_transfers:
                break

            for source_component_id in source_component_ids:
                if gap <= 0 or len(emergency_transfers) >= max_emergency_transfers:
                    break

                source_component = component_by_id.get(source_component_id)

                if source_component is None:
                    continue

                max_transport_minutes = float(
                    source_component.max_transport_hours or 24.0
                ) * 60.0

                candidate_sources = sorted(
                    facility_ids,
                    key=lambda source_id: route_travel_minutes(
                        source_id, dest_facility_id
                    ),
                )

                for source_facility_id in candidate_sources:
                    # The cap has to be tested here too. Checking it only in the
                    # outer loops let the count overshoot (22 against a cap of
                    # 20 on the reference scenario).
                    if len(emergency_transfers) >= max_emergency_transfers:
                        break

                    if source_facility_id == dest_facility_id:
                        continue

                    if not sharing_allowed(source_facility_id, dest_facility_id):
                        continue

                    supply = remaining_supply.get(
                        (source_facility_id, source_component_id, donor_group_id),
                        0,
                    )

                    if supply <= 0:
                        continue

                    travel = route_travel_minutes(
                        source_facility_id, dest_facility_id
                    )

                    if travel > max_transport_minutes:
                        continue

                    units = min(int(gap), int(supply))

                    if units <= 0:
                        continue

                    emergency_transfers.append(
                        {
                            "from_facility_id": source_facility_id,
                            "from_facility_name": facility_name_map.get(
                                source_facility_id
                            ),
                            "to_facility_id": dest_facility_id,
                            "to_facility_name": facility_name_map.get(
                                dest_facility_id
                            ),
                            "component_id": int(source_component_id),
                            "component_code": component_code_map.get(
                                source_component_id
                            ),
                            "blood_group_id": int(donor_group_id),
                            "blood_group_code": group_code_map.get(donor_group_id),
                            "recipient_group_id": int(recipient_group_id),
                            "recipient_group_code": group_code_map.get(
                                recipient_group_id
                            ),
                            "units": int(units),
                            "travel_minutes": int(travel),
                        }
                    )

                    remaining_supply[
                        (source_facility_id, source_component_id, donor_group_id)
                    ] = (supply - units)

                    gap -= units

                    if gap <= 0:
                        break

        remaining_gap[series_key] = gap

    remaining_gap_by_cg = defaultdict(int)
    total_remaining_gap = 0

    for series_key, gap_value in remaining_gap.items():
        if gap_value > 0:
            _, component_id, group_id = series_key
            remaining_gap_by_cg[(component_id, group_id)] += int(gap_value)
            total_remaining_gap += int(gap_value)

    for idx, key in enumerate(cg_keys):
        unplaced_gap = int(math.ceil(float(unplaced_cg_p95[idx])))
        if unplaced_gap > 0:
            remaining_gap_by_cg[key] += unplaced_gap
            total_remaining_gap += unplaced_gap

    for item in requirement_by_group_component:
        cg_key = (item["component_id"], item["blood_group_id"])

        final_gap = int(remaining_gap_by_cg.get(cg_key, 0))

        item["gap"] = final_gap
        item["covered"] = max(0, item["required_p95"] - final_gap)

    network_can_supply = sum(
        item["covered"] for item in requirement_by_group_component
    )

    final_gap_by_group = defaultdict(int)

    for (_, group_id), gap_value in remaining_gap_by_cg.items():
        final_gap_by_group[group_id] += int(gap_value)

    donor_mobilization = []

    for group_id, gap_units in final_gap_by_group.items():
        if gap_units <= 0:
            continue

        effective_yield = (
            DONOR_YIELD_PER_DONOR
            * DONOR_SHOW_UP_RATE
            * DONOR_ELIGIBILITY_RATE
        )

        donors_needed = math.ceil(gap_units / max(0.01, effective_yield))

        donor_mobilization.append(
            {
                "blood_group_id": int(group_id),
                "blood_group_code": group_code_map.get(group_id),
                "gap_units": int(gap_units),
                "donors_needed": int(donors_needed),
            }
        )

    donor_mobilization.sort(
        key=lambda item: item["donors_needed"],
        reverse=True,
    )

    timeline = []

    weights = onset_weights(onset_profile, duration_hours)

    # Time-to-critical is a question about the network as it stands right now,
    # not after a plan nobody has approved yet.
    supply_available_total = facility_covered_before

    cumulative_weight = 0.0
    time_to_critical_minutes = None

    for hour, weight in enumerate(weights):
        cumulative_weight += float(weight)

        cumulative_demand_p50 = int(round(units_required_p50 * cumulative_weight))
        cumulative_demand_p95 = int(round(units_required_p95 * cumulative_weight))

        if (
            time_to_critical_minutes is None
            and cumulative_demand_p95 > supply_available_total
        ):
            time_to_critical_minutes = hour * 60

        timeline.append(
            {
                "hour": int(hour),
                "cumulative_demand_p50": cumulative_demand_p50,
                "cumulative_demand_p95": cumulative_demand_p95,
                "supply_available": supply_available_total,
            }
        )

    def as_coverage(supplied: int, required: int) -> float:
        if required <= 0:
            return 100.0

        return round(min(100.0, 100.0 * supplied / float(required)), 1)

    # Both measured per facility against local stock, so they are comparable.
    facility_covered_after = facility_requirement_total - total_remaining_gap

    coverage_before_pct = as_coverage(
        facility_covered_before, facility_requirement_total
    )
    coverage_after_pct = as_coverage(
        facility_covered_after, facility_requirement_total
    )

    infrastructure["affected_facilities"] = [
        {
            "id": facility_id,
            "name": facility_name_map.get(facility_id),
            "distance_km": infrastructure["facility_distances_km"].get(facility_id),
            "degraded": facility_id in degraded_facilities,
        }
        for facility_id in infrastructure["affected_facility_ids"]
    ]
    infrastructure["network_surge_capacity_casualties"] = int(
        surge_capacities.sum()
    )
    infrastructure["unplaced_casualties"] = int(unplaced_casualties)
    reserve_units_released = max(
        0,
        sum(facility_supply.values()) - sum(current_facility_supply.values()),
    )
    duration_ms = int(round((time_module.perf_counter() - started) * 1000))

    results = {
        "scenario": scenario,
        "generated_at": DEMO_DATETIME.isoformat(),
        "totals": {
            "casualties": casualties,
            "iterations": iterations,
            # True Monte Carlo distribution of the network-wide total.
            "units_required_p50": units_required_p50,
            "units_required_p95": units_required_p95,
            "monte_carlo_total_p50": monte_carlo_total_p50,
            "monte_carlo_total_p95": monte_carlo_total_p95,
            # Conservative per-group sum. Higher than the Monte Carlo P95 by
            # construction; this is the figure to plan procurement against.
            "planning_requirement_p50": planning_requirement_p50,
            "planning_requirement_p95": planning_requirement_p95,
            # Facility-level requirement and coverage, before any action and
            # after the emergency transfer plan. Both on the same basis.
            "facility_requirement_p95": facility_requirement_total,
            "network_can_supply_now": facility_covered_before,
            "network_can_supply_after_plan": facility_covered_after,
            "coverage_before_actions_pct": coverage_before_pct,
            "coverage_after_actions_pct": coverage_after_pct,
            "gap_units_now": max(
                0, facility_requirement_total - facility_covered_before
            ),
            "gap_units_after_plan": max(
                0, facility_requirement_total - facility_covered_after
            ),
            "time_to_critical_minutes": time_to_critical_minutes,
            "emergency_transfers": len(emergency_transfers),
            "unplaced_casualties": int(unplaced_casualties),
            "unplaced_requirement_p95": int(unplaced_requirement_p95),
        },
        "duration_ms": duration_ms,
        "requirement_by_group_component": requirement_by_group_component,
        "facility_requirements": facility_requirements[:100],
        "emergency_transfers": emergency_transfers,
        "donor_mobilization": donor_mobilization,
        "timeline": timeline,
        "infrastructure": infrastructure,
        "interventions": {
            "emergency_reserves_released": scenario[
                "release_emergency_reserves"
            ],
            "emergency_reserve_release_pct": scenario[
                "emergency_reserve_release_pct"
            ],
            "reserve_units_made_available": int(reserve_units_released),
            "roads_blocked": scenario["roads_blocked"],
            "road_travel_multiplier": (
                ROAD_BLOCK_MULTIPLIER if scenario["roads_blocked"] else 1.0
            ),
        },
        "brief_facts": {
            "event_name": scenario.get("name", "Emergency scenario"),
            "event_type": event_type,
            "casualties": casualties,
            "units_required_p50": units_required_p50,
            "units_required_p95": units_required_p95,
            "planning_requirement_p95": planning_requirement_p95,
            "network_can_supply_now": facility_covered_before,
            "coverage_before_actions_pct": coverage_before_pct,
            "coverage_after_actions_pct": coverage_after_pct,
            "gap_units_now": max(
                0, facility_requirement_total - facility_covered_before
            ),
            "emergency_transfers": len(emergency_transfers),
            "donor_mobilization_groups": len(donor_mobilization),
            "time_to_critical_minutes": time_to_critical_minutes,
        },
    }

    if comparison_base:
        base_totals = comparison_base.get("totals") or {}
        comparison_fields = (
            "coverage_after_actions_pct",
            "gap_units_after_plan",
            "emergency_transfers",
            "time_to_critical_minutes",
        )
        deltas = {}
        for field in comparison_fields:
            current = results["totals"].get(field)
            baseline = base_totals.get(field)
            deltas[field] = (
                round(float(current) - float(baseline), 1)
                if current is not None and baseline is not None
                else None
            )
        results["comparison"] = {
            "base_run_id": comparison_base.get("run_id"),
            "same_seed": int(
                (comparison_base.get("scenario") or {}).get("seed", -1)
            )
            == seed,
            "deltas": deltas,
        }

    from services.narrative_service import incident_brief

    results["brief_en"] = incident_brief(results, scenario, language="en")
    results["brief_ur"] = incident_brief(results, scenario, language="ur")

    if save:
        accountable_actor = actor or Actor(
            user_id="system:simulation",
            display_name="System (simulation)",
            role="SYSTEM_ADMIN",
            organization_id=scenario.get("organization_id"),
        )
        run = SimulationRun(
            id=new_id(),
            organization_id=accountable_actor.organization_id,
            created_by=accountable_actor.display_name,
            mode="PREPAREDNESS",
            parent_run_id=parent_run_id,
            duration_ms=duration_ms,
            name=str(scenario.get("name", "Emergency simulation")),
            event_type=event_type,
            seed=seed,
            iterations=iterations,
            scenario_json=scenario,
            results_json={},
            status="COMPLETED",
        )

        with audited(
            session,
            accountable_actor,
            "simulation.complete",
            "simulation_run",
            run.id,
        ) as entry:
            session.add(run)
            results["run_id"] = run.id
            run.results_json = results
            entry.on(run, after=snapshot(run, SIMULATION_FIELDS))
            entry.note(
                casualties=casualties,
                degraded_facilities=len(degraded_facilities),
                reserve_units_released=reserve_units_released,
            )

    return results


def _run_visible(run: SimulationRun, actor: Actor) -> bool:
    if actor.role in {"SYSTEM_ADMIN", "PROVINCIAL_ADMIN", "EMERGENCY_CONTROLLER"}:
        return True
    return bool(actor.organization_id and run.organization_id == actor.organization_id)


def get_simulation_run(session, actor: Actor, run_id: str) -> SimulationRun:
    run = session.get(SimulationRun, run_id)
    if run is None or not _run_visible(run, actor):
        raise ServiceError(
            "SIMULATION_NOT_FOUND",
            "Simulation run was not found in this organization scope.",
        )
    return run


def list_simulation_runs(session, actor: Actor, *, limit: int = 20) -> list[SimulationRun]:
    statement = select(SimulationRun).order_by(SimulationRun.created_at.desc()).limit(
        max(1, min(int(limit), 100))
    )
    if actor.role not in {
        "SYSTEM_ADMIN",
        "PROVINCIAL_ADMIN",
        "EMERGENCY_CONTROLLER",
    }:
        if not actor.organization_id:
            return []
        statement = statement.where(
            SimulationRun.organization_id == actor.organization_id
        )
    return list(session.scalars(statement).all())


def compare_simulation(
    session,
    actor: Actor,
    base_run_id: str,
    interventions: dict,
) -> dict:
    """Run an intervention against exactly the same clinical random stream."""

    require(actor, Permission.RUN_SIMULATION, "compare emergency simulations")
    base = get_simulation_run(session, actor, base_run_id)
    allowed = {
        "name",
        "facilities_degraded_pct",
        "degraded_capacity_loss_pct",
        "roads_blocked",
        "release_emergency_reserves",
        "emergency_reserve_release_pct",
    }
    scenario = dict(base.scenario_json or {})
    scenario.update(
        {key: value for key, value in (interventions or {}).items() if key in allowed}
    )
    scenario["seed"] = base.seed
    scenario["iterations"] = base.iterations
    if not scenario.get("name"):
        scenario["name"] = f"{base.name} — intervention"
    base_results = dict(base.results_json or {})
    base_results.setdefault("run_id", base.id)
    return run_simulation(
        session,
        scenario,
        save=True,
        actor=actor,
        parent_run_id=base.id,
        comparison_base=base_results,
    )
