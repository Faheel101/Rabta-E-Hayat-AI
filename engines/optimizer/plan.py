"""Network transfer optimizer (spec §8.1).

One mixed-integer model covering both objectives the product exists to trade off:
eliminate predicted shortage, and rescue units that would otherwise expire.

The previous implementation solved these in two disconnected passes — a greedy
rescue heuristic, then a MILP for shortage, then a post-solve `cap_shipments`
that discarded moves the solver had already counted as satisfying demand. The
plan's stored impact therefore contradicted the plan's contents: it claimed 343
rescued plus 261 shortage-averted units while persisting 284 units in total, and
its "shortages averted" figure was computed before the truncation that removed
some of them. Truncating a solved optimisation also silently discards optimality:
the solver chose those twelve routes on the assumption that all of them ran.

Shipment consolidation is a constraint inside the model (constraint 8), so the
solver trades route count against coverage itself, and the plan that comes out is
the plan that gets stored.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, time, timedelta

from ortools.sat.python import cp_model

from core import config

URGENCY_WEIGHT = {"CRITICAL": 3.0, "WARNING": 2.0, "WATCH": 1.2, "SAFE": 1.0}


def weights(overrides: dict | None = None) -> dict:
    configured = config.get("optimizer.weights") or {}
    configured = {**configured, **(overrides or {})}

    return {
        "shortage": float(configured.get("shortage", 1000)),
        "waste": float(configured.get("waste", 200)),
        "transport": float(configured.get("transport", 1)),
        "fixed_dispatch": float(configured.get("fixed_dispatch", 25)),
        "substitution": float(configured.get("substitution", 15)),
        "capacity": float(configured.get("capacity", 50)),
    }


def arrival_is_feasible(
    facility,
    departure: datetime,
    travel_minutes: int,
) -> bool:
    """Constraint 10: arrival must fall inside the receiving facility's hours."""

    hours = facility.operating_hours_json or {}

    if hours.get("24_7"):
        return True

    window = hours.get("hours")

    if not window or "-" not in str(window):
        return True

    start_text, end_text = str(window).split("-", 1)

    try:
        opens = time.fromisoformat(start_text.strip())
        closes = time.fromisoformat(end_text.strip())
    except ValueError:
        return True

    arrival = (departure + timedelta(minutes=travel_minutes)).timetz()

    return opens <= arrival.replace(tzinfo=None) <= closes


def build_candidates(
    *,
    deficits,
    surpluses,
    at_risk,
    donors_for_recipient,
    travel_minutes,
    facilities_by_id,
    components_by_id,
    departure: datetime,
    max_sources_per_deficit: int = 0,
    log=print,
):
    """Feasible (source, destination, component, donor group, recipient group).

    Pre-filtering to pairs inside the transport limit where the source has
    surplus and the destination has risk typically removes over 90% of pairs
    (spec §8.3), which is what keeps the solve inside its time limit.
    """

    surplus_index = defaultdict(list)
    self_supplying = 0

    for (facility_id, component_id, group_id), units in surpluses.items():
        if units <= 0:
            continue

        # A facility in deficit for a series must never be a source of it.
        #
        # Surplus and deficit are measured differently — surplus is stock above
        # the reserve floor today, deficit is projected shortfall against the
        # forecast — so the same facility can appear in both sets for the same
        # series, and nothing downstream noticed. The plan then shipped three
        # units of B- red cells into Faisalabad from Lahore and four units of
        # B- red cells out of Faisalabad to Rawalpindi on the same day: two
        # lorries, two validated cold boxes, one unit worse off than doing
        # nothing.
        #
        # Skipping a self-supplying source here rather than adding a solver
        # constraint keeps it out of the model entirely, so it also cannot be
        # chosen as a fallback when the solve gets tight.
        if deficits.get((facility_id, component_id, group_id), 0) > 0:
            self_supplying += 1
            continue

        surplus_index[(component_id, group_id)].append((facility_id, units))

    if self_supplying:
        log(
            f"  excluded {self_supplying:,} facility-series from the source pool: "
            f"each is itself forecast short of the series it would have shipped"
        )

    candidates = []
    dropped = 0

    for (dest_id, component_id, recipient_group_id), deficit in deficits.items():
        if deficit <= 0:
            continue

        component = components_by_id.get(component_id)
        destination = facilities_by_id.get(dest_id)

        if component is None or destination is None:
            continue

        limit_minutes = float(component.max_transport_hours or 24.0) * 60.0

        for donor_group_id, rank in donors_for_recipient.get(
            (component_id, recipient_group_id), []
        ):
            feasible = []

            for source_id, available in surplus_index.get(
                (component_id, donor_group_id), []
            ):
                if source_id == dest_id:
                    continue

                minutes = travel_minutes.get((source_id, dest_id))

                if minutes is None or minutes > limit_minutes:
                    continue

                if not arrival_is_feasible(destination, departure, minutes):
                    continue

                upper_bound = min(int(available), int(deficit))

                if upper_bound <= 0:
                    continue

                feasible.append((minutes, source_id, upper_bound))

            # Keep only the nearest sources. A twentieth-choice supplier four
            # hours away is never in an optimal plan, but it still costs the
            # solver a variable, and the difference decides whether the solve
            # proves optimality inside its time limit or returns whatever it had.
            feasible.sort()

            if max_sources_per_deficit > 0 and len(feasible) > max_sources_per_deficit:
                dropped += len(feasible) - max_sources_per_deficit
                feasible = feasible[:max_sources_per_deficit]

            for minutes, source_id, upper_bound in feasible:
                candidates.append(
                    {
                        "source_id": source_id,
                        "dest_id": dest_id,
                        "component_id": component_id,
                        "donor_group_id": donor_group_id,
                        "recipient_group_id": recipient_group_id,
                        "preference_rank": int(rank),
                        "upper_bound": upper_bound,
                        "travel_minutes": int(minutes),
                    }
                )

    if dropped:
        # Never let a bound on coverage go unreported (it would read as
        # "everything was considered" when it was not).
        log(
            f"  pre-filter kept the {max_sources_per_deficit} nearest sources per "
            f"deficit; {dropped:,} more distant candidate routes were excluded"
        )

    return candidates


def shortlist_route_pairs(
    *,
    candidates,
    deficits,
    risk_buckets,
    criticality,
    at_risk,
    max_pairs: int,
    min_pairs_per_destination: int = 3,
    log=print,
):
    """Bound interactive solve breadth without weakening clinical constraints.

    The expensive choice in this model is which physical source/destination
    routes to activate, not the number of units on a chosen route. Keeping ten
    times the dispatch allowance is ample choice for a 12-shipment plan while
    avoiding hundreds of route Boolean variables that CP-SAT must compare.

    Routes are ranked by the shortage severity they can cover, compatibility
    preference, travel time, and expiring stock they can rescue. A per-
    destination floor is selected before the province-wide ranking so a large
    hospital cannot crowd smaller urgent destinations out of the model. This is
    candidate generation only: reserve floors, demand, storage, compatibility,
    and shipment limits remain hard constraints in :func:`solve`.
    """

    grouped = defaultdict(list)

    for candidate in candidates:
        grouped[(candidate["source_id"], candidate["dest_id"])].append(candidate)

    available_pairs = len(grouped)

    if max_pairs <= 0 or available_pairs <= max_pairs:
        return list(candidates), {
            "available_route_pairs": available_pairs,
            "selected_route_pairs": available_pairs,
            "excluded_route_pairs": 0,
        }

    scores = {}

    for pair, pair_candidates in grouped.items():
        # Multiple donor groups may serve the same recipient demand. Count only
        # the strongest path for that demand so promiscuous compatibility does
        # not artificially inflate a route's rank.
        shortage_by_demand = {}
        rescue_by_supply = {}

        for candidate in pair_candidates:
            demand_key = (
                candidate["dest_id"],
                candidate["component_id"],
                candidate["recipient_group_id"],
            )
            supply_key = (
                candidate["source_id"],
                candidate["component_id"],
                candidate["donor_group_id"],
            )
            deficit = int(deficits.get(demand_key, 0))
            cover = min(deficit, int(candidate["upper_bound"]))
            urgency = URGENCY_WEIGHT.get(
                risk_buckets.get(demand_key, "WATCH"), 1.0
            )
            component_weight = float(
                criticality.get(candidate["component_id"], 1.0)
            )
            preference_penalty = 1.0 + 0.15 * max(
                0, int(candidate["preference_rank"]) - 1
            )
            travel_penalty = 1.0 + float(candidate["travel_minutes"]) / 240.0
            clinical_score = (
                cover
                * urgency
                * component_weight
                / preference_penalty
                / travel_penalty
            )
            shortage_by_demand[demand_key] = max(
                shortage_by_demand.get(demand_key, 0.0), clinical_score
            )

            expiring = min(
                int(at_risk.get(supply_key, 0)), int(candidate["upper_bound"])
            )
            rescue_score = expiring * component_weight / travel_penalty
            rescue_by_supply[supply_key] = max(
                rescue_by_supply.get(supply_key, 0.0), rescue_score
            )

        # Shortage avoidance is the primary objective. Expiry rescue is a
        # meaningful tie-breaker at one fifth of the route-ranking weight,
        # matching the 1000:200 objective relationship in the configured model.
        scores[pair] = sum(shortage_by_demand.values()) + 0.2 * sum(
            rescue_by_supply.values()
        )

    def rank_key(pair):
        # Source and destination IDs make ties deterministic across processes.
        return (-scores[pair], pair[0], pair[1])

    by_destination = defaultdict(list)

    for pair in grouped:
        by_destination[pair[1]].append(pair)

    selected = set()
    destination_floor = max(0, int(min_pairs_per_destination))

    for destination_id in sorted(by_destination):
        selected.update(
            sorted(by_destination[destination_id], key=rank_key)[:destination_floor]
        )

    # In an unusually wide network the requested destination floor can exceed
    # the global cap. Preserve the globally strongest members of the floor.
    if len(selected) > max_pairs:
        selected = set(sorted(selected, key=rank_key)[:max_pairs])
    else:
        remaining = sorted(
            (pair for pair in grouped if pair not in selected), key=rank_key
        )
        selected.update(remaining[: max_pairs - len(selected)])

    filtered = [
        candidate
        for candidate in candidates
        if (candidate["source_id"], candidate["dest_id"]) in selected
    ]
    excluded = available_pairs - len(selected)
    log(
        f"  interactive shortlist kept {len(selected):,} of "
        f"{available_pairs:,} physical route pairs "
        f"({len(filtered):,} clinical candidate paths); "
        f"{excluded:,} lower-ranked route pairs were excluded"
    )

    return filtered, {
        "available_route_pairs": available_pairs,
        "selected_route_pairs": len(selected),
        "excluded_route_pairs": excluded,
    }


def solve(
    *,
    candidates,
    deficits,
    surpluses,
    at_risk,
    risk_buckets,
    criticality,
    storage_headroom,
    max_shipments: int,
    time_limit_seconds: int,
    weight_overrides: dict | None = None,
    log=print,
):
    """Solve the transfer MILP. Returns (moves, diagnostics)."""

    w = weights(weight_overrides)
    model = cp_model.CpModel()

    variables = []
    by_supply = defaultdict(list)
    by_component_out = defaultdict(list)
    by_demand = defaultdict(list)
    by_pair = defaultdict(list)
    by_inflow_component = defaultdict(list)
    pair_bound = defaultdict(int)

    objective = []

    for index, candidate in enumerate(candidates):
        variable = model.NewIntVar(0, candidate["upper_bound"], f"x{index}")
        variables.append((candidate, variable))

        by_supply[
            (
                candidate["source_id"],
                candidate["component_id"],
                candidate["donor_group_id"],
            )
        ].append(variable)

        by_component_out[
            (candidate["source_id"], candidate["component_id"])
        ].append(variable)

        by_demand[
            (
                candidate["dest_id"],
                candidate["component_id"],
                candidate["recipient_group_id"],
            )
        ].append(variable)

        pair = (candidate["source_id"], candidate["dest_id"])
        by_pair[pair].append(variable)
        pair_bound[pair] += candidate["upper_bound"]

        by_inflow_component[
            (candidate["dest_id"], candidate["component_id"])
        ].append(variable)

        # Transport cost, scaled to keep coefficients integral.
        objective.append(
            int(round(w["transport"] * candidate["travel_minutes"] / 10.0)) * variable
        )

        substitution = candidate["preference_rank"] - 1

        if substitution > 0:
            objective.append(
                int(round(w["substitution"] * substitution)) * variable
            )

    # Constraint 1: supply availability, never below the clinical reserve.
    for key, group in by_supply.items():
        model.Add(sum(group) <= int(surpluses.get(key, 0)))

    # Constraint 2: demand satisfaction, soft via unmet.
    unmet_vars = {}

    for key, deficit in deficits.items():
        if deficit <= 0:
            continue

        unmet = model.NewIntVar(0, int(deficit), f"s_{len(unmet_vars)}")
        unmet_vars[key] = unmet

        model.Add(sum(by_demand.get(key, [])) + unmet >= int(deficit))

        bucket = risk_buckets.get(key, "WATCH")
        weight = (
            w["shortage"]
            * URGENCY_WEIGHT.get(bucket, 1.0)
            * criticality.get(key[1], 1.0)
        )
        objective.append(int(round(weight)) * unmet)

    # Waste: units at risk of expiry that are not moved out.
    waste_vars = {}

    for key, units in at_risk.items():
        if units <= 0:
            continue

        waste = model.NewIntVar(0, int(units), f"w_{len(waste_vars)}")
        waste_vars[key] = waste

        outflow = by_supply.get(key, [])
        model.Add(waste >= int(units) - sum(outflow))

        weight = w["waste"] * criticality.get(key[1], 1.0)
        objective.append(int(round(weight)) * waste)

    # Constraint 6: receiving capacity.
    for key, group in by_inflow_component.items():
        headroom = storage_headroom.get(key)

        if headroom is not None:
            model.Add(sum(group) <= max(0, int(headroom)))

    # Constraint 7 and 8: dispatch linking and shipment consolidation.
    shipment_vars = []

    for pair, group in by_pair.items():
        bound = pair_bound[pair]

        if bound <= 0:
            continue

        runs = model.NewBoolVar(f"y_{pair[0][:8]}_{pair[1][:8]}")
        model.Add(sum(group) <= bound * runs)

        objective.append(int(round(w["fixed_dispatch"])) * runs)
        shipment_vars.append(runs)

    if shipment_vars:
        model.Add(sum(shipment_vars) <= max_shipments)

    model.Minimize(sum(objective))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    # OR-Tools and pandas/pyarrow both bundle Abseil on macOS. CP-SAT's
    # multi-worker coordinator can bind to Arrow's synchronization symbols and
    # sleep forever; the sequential engine avoids that native-library collision
    # and, with the route shortlist above, remains comfortably interactive.
    solver.parameters.num_search_workers = int(
        config.get("optimizer.num_search_workers", 1)
    )

    status = solver.Solve(model)
    status_name = solver.StatusName(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # Spec §8.3: never return "no solution". Demand satisfaction is already
        # soft, so an infeasible result means a modelling error, not an
        # impossible network.
        log(f"  solver returned {status_name}; returning empty plan")
        return [], {
            "status": status_name,
            "total_deficit": int(sum(deficits.values())),
            "unmet_demand": int(sum(deficits.values())),
            "shortages_averted": 0,
            "units_at_risk": int(sum(at_risk.values())),
            "units_rescued": 0,
            "shipments": 0,
        }

    moves = []

    for candidate, variable in variables:
        units = int(solver.Value(variable))

        if units > 0:
            moves.append({**candidate, "units": units})

    total_deficit = int(sum(deficits.values()))
    unmet_total = int(sum(solver.Value(v) for v in unmet_vars.values()))
    waste_total = int(sum(solver.Value(v) for v in waste_vars.values()))
    at_risk_total = int(sum(at_risk.values()))

    objective_value = solver.ObjectiveValue()
    best_bound = solver.BestObjectiveBound()

    # Report how far from proven-optimal this plan is. A plan returned at the
    # time limit is still the best found, but the user is entitled to know it was
    # not proved optimal rather than being told "OPTIMAL" by omission.
    if objective_value and abs(objective_value) > 1e-9:
        optimality_gap = abs(objective_value - best_bound) / abs(objective_value)
    else:
        optimality_gap = 0.0

    diagnostics = {
        "status": status_name,
        "objective": objective_value,
        "best_bound": best_bound,
        "optimality_gap": round(optimality_gap, 4),
        "wall_time_seconds": round(solver.WallTime(), 2),
        "total_deficit": total_deficit,
        "unmet_demand": unmet_total,
        "shortages_averted": total_deficit - unmet_total,
        "units_at_risk": at_risk_total,
        "units_rescued": at_risk_total - waste_total,
        "units_still_wasted": waste_total,
        "shipments": len({(m["source_id"], m["dest_id"]) for m in moves}),
        "candidates_considered": len(candidates),
    }

    return moves, diagnostics
