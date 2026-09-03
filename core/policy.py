"""Reserve floor and storage capacity policy (spec §4.2, §8.1 constraint 1).

The reserve floor is the constraint that makes this tool politically acceptable
to a hospital administrator: the optimizer may never drain a facility below it.
It therefore has to mean the same thing in every engine.

It previously did not. `min_reserve_policy_json` held one number per component,
and the risk engine added that number to each of the eight blood group series
(so a 40-unit PRBC policy demanded 320 units of reserve), while the optimizer
scaled it by national population share (so a 1,000-bed trauma centre carried a
1-unit O- floor). Both are wrong in opposite directions.

This module resolves a floor per component x group from one documented policy,
weighting universal-donor groups above their population share and protecting
them with absolute minimums.
"""

from __future__ import annotations

from core import config

RED_COMPONENTS = {"WB", "PRBC"}
PLASMA_COMPONENTS = {"FFP", "CRYO"}
PLATELET_COMPONENTS = {"PLT_RD", "PLT_APH"}


def facility_policy(facility_type: str) -> tuple[dict, dict, dict]:
    """Default capacity, component reserve totals and operating hours.

    This is the one policy contract used both by synthetic reference seeding
    and guided onboarding. A newly activated facility must satisfy the same
    optimizer and shortage-risk invariants as every seeded facility.
    """

    policies = {
        "RBC": (
            {"WB": 100, "PRBC": 2600, "PLT_RD": 500, "PLT_APH": 150, "FFP": 1400, "CRYO": 400},
            {"WB": 10, "PRBC": 120, "PLT_RD": 30, "PLT_APH": 10, "FFP": 60, "CRYO": 20},
            {"24_7": True},
        ),
        "TERTIARY_HOSPITAL": (
            {"WB": 30, "PRBC": 1400, "PLT_RD": 240, "PLT_APH": 80, "FFP": 700, "CRYO": 200},
            {"WB": 4, "PRBC": 40, "PLT_RD": 12, "PLT_APH": 4, "FFP": 20, "CRYO": 8},
            {"24_7": True},
        ),
        "SPECIALIST_CENTRE": (
            {"WB": 10, "PRBC": 550, "PLT_RD": 160, "PLT_APH": 55, "FFP": 260, "CRYO": 90},
            {"WB": 2, "PRBC": 25, "PLT_RD": 10, "PLT_APH": 4, "FFP": 12, "CRYO": 5},
            {"24_7": False, "hours": "08:00-20:00"},
        ),
        "DHQ": (
            {"WB": 10, "PRBC": 260, "PLT_RD": 60, "PLT_APH": 20, "FFP": 120, "CRYO": 40},
            {"WB": 2, "PRBC": 16, "PLT_RD": 4, "PLT_APH": 2, "FFP": 8, "CRYO": 3},
            {"24_7": True},
        ),
        "THQ": (
            {"WB": 5, "PRBC": 90, "PLT_RD": 20, "PLT_APH": 6, "FFP": 40, "CRYO": 12},
            {"WB": 1, "PRBC": 6, "PLT_RD": 2, "PLT_APH": 1, "FFP": 3, "CRYO": 1},
            {"24_7": False, "hours": "08:00-20:00"},
        ),
    }
    capacity, reserve, operating = policies.get(facility_type, policies["THQ"])
    return dict(capacity), dict(reserve), dict(operating)


def component_family(component_code: str) -> str:
    if component_code in RED_COMPONENTS:
        return "red"

    if component_code in PLASMA_COMPONENTS:
        return "plasma"

    if component_code in PLATELET_COMPONENTS:
        return "platelet"

    return "red"


def build_group_shares(groups) -> dict[str, float]:
    """Population share by blood group code, normalised to sum to 1."""

    total = sum(float(group.population_pct_pk or 0.0) for group in groups)

    if total <= 0:
        count = max(1, len(groups))
        return {group.code: 1.0 / count for group in groups}

    return {
        group.code: float(group.population_pct_pk or 0.0) / total
        for group in groups
    }


def _strategic_minimum(
    family: str,
    group_code: str,
    facility_type: str,
) -> float:
    table = config.get("reserve_policy.strategic_minimum_units") or {}
    by_group = table.get(family) or {}
    by_type = by_group.get(group_code) or {}

    return float(by_type.get(facility_type, 0) or 0)


def strategic_minimum_total(
    component_code: str,
    facility_type: str,
    group_codes,
) -> float:
    """Sum of the absolute floors for a component at a facility type.

    These override the aggregate ceiling by design: a Regional Blood Centre holds
    its O- emergency stock whatever its headline reserve policy says.
    """

    family = component_family(component_code)

    return sum(
        _strategic_minimum(family, code, facility_type) for code in group_codes
    )


def allocate_group_floors(
    component_total: float,
    component_code: str,
    facility_type: str,
    group_shares: dict[str, float],
) -> dict[str, float]:
    """Split a component-level reserve total across blood groups.

    Returns integer unit floors keyed by blood group code. The sum is held close
    to `component_total` so that expanding a policy across eight groups cannot
    silently multiply a facility's obligation.
    """

    family = component_family(component_code)

    demand_weight = float(config.get("reserve_policy.demand_share_weight", 0.7))
    strategic_weight = float(
        config.get("reserve_policy.strategic_share_weight", 0.3)
    )

    strategic_shares = (
        config.get("reserve_policy.strategic_shares") or {}
    ).get(family) or {}

    minimums = {
        code: _strategic_minimum(family, code, facility_type)
        for code in group_shares
    }

    if component_total <= 0:
        return {code: float(minimums[code]) for code in group_shares}

    weights = {}

    for code, share in group_shares.items():
        strategic = float(strategic_shares.get(code, share))
        weights[code] = demand_weight * float(share) + strategic_weight * strategic

    weight_total = sum(weights.values())

    if weight_total <= 0:
        weights = {code: 1.0 / max(1, len(group_shares)) for code in group_shares}
        weight_total = 1.0

    floors = {
        code: max(
            component_total * (weight / weight_total),
            minimums[code],
        )
        for code, weight in weights.items()
    }

    max_inflation = float(
        config.get("reserve_policy.max_aggregate_inflation", 1.20)
    )
    ceiling = component_total * max_inflation
    allocated = sum(floors.values())

    if allocated > ceiling:
        headroom = {code: floors[code] - minimums[code] for code in floors}
        total_headroom = sum(headroom.values())

        if total_headroom > 0:
            excess = allocated - ceiling

            for code in floors:
                reduction = excess * (headroom[code] / total_headroom)
                floors[code] = max(minimums[code], floors[code] - reduction)

    return {code: float(round(value)) for code, value in floors.items()}


def expand_reserve_policy(
    component_totals: dict[str, float],
    facility_type: str,
    group_shares: dict[str, float],
) -> dict[str, dict[str, float]]:
    """Build the stored `min_reserve_policy_json`: component -> group -> units."""

    return {
        component_code: allocate_group_floors(
            float(total or 0.0),
            component_code,
            facility_type,
            group_shares,
        )
        for component_code, total in component_totals.items()
    }


def reserve_floor(
    facility,
    component_code: str,
    group_code: str,
    group_shares: dict[str, float] | None = None,
) -> float:
    """The floor for one (facility, component, group). The single lookup.

    Accepts either the expanded per-group policy or a legacy component-level
    number, so engines behave correctly before and after a re-seed.
    """

    policy = facility.min_reserve_policy_json or {}
    entry = policy.get(component_code)

    if entry is None:
        return 0.0

    if isinstance(entry, dict):
        if group_code in entry:
            return float(entry.get(group_code) or 0.0)

        # A legacy {"default": n} shape, or a policy written before this group
        # existed. Fall through to allocation rather than returning the total.
        if "default" in entry and len(entry) == 1:
            entry = entry["default"]
        else:
            return 0.0

    if group_shares is None:
        return 0.0

    floors = allocate_group_floors(
        float(entry or 0.0),
        component_code,
        str(facility.facility_type),
        group_shares,
    )

    return float(floors.get(group_code, 0.0))


def component_reserve_total(facility, component_code: str) -> float:
    """Sum of a component's group floors — the facility-level obligation."""

    policy = facility.min_reserve_policy_json or {}
    entry = policy.get(component_code)

    if entry is None:
        return 0.0

    if isinstance(entry, dict):
        if len(entry) == 1 and "default" in entry:
            return float(entry["default"] or 0.0)

        return float(sum(float(value or 0.0) for value in entry.values()))

    return float(entry or 0.0)


def storage_capacity(facility, component_code: str) -> float:
    capacity = facility.storage_capacity_json or {}
    value = capacity.get(component_code)

    if isinstance(value, dict):
        value = value.get("default", 0)

    return float(value or 0.0)


def lead_time_days(facility) -> int:
    table = config.get("risk.lead_time_days") or {}
    default = int(table.get("default", 2))

    return int(table.get(str(facility.facility_type), default))
