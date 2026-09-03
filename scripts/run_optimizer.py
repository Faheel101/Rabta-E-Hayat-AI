"""Network transfer plan (spec §8).

    python -m scripts.run_optimizer

Deficits and surpluses are read from the shortage_risk table rather than
recomputed here, so the plan acts on exactly the numbers the Command Centre
displays. Two engines deriving "how short is this facility" independently is how
a dashboard and its recommendations end up disagreeing in front of a user.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone

# Load OR-Tools before pandas/pyarrow. Both ship Abseil synchronization symbols
# on macOS; loading Arrow first can interpose its version into CP-SAT, leaving
# the parallel solver asleep on a condition variable forever.
from engines.optimizer.plan import (
    build_candidates,
    shortlist_route_pairs,
    solve,
    weights,
)
import pandas as pd
from sqlalchemy import insert, select

from config.settings import DEMO_DATE
from core import config, geo, policy
from db.models import (
    AuditLog,
    BloodGroup,
    BloodUnit,
    Compatibility,
    Component,
    ExpiryRescue,
    Facility,
    Organization,
    PlatformSetting,
    ShortageRisk,
    Transfer,
    TransferPlan,
    new_id,
)
from db.session import SessionLocal, init_db
from engines.optimizer.unit_select import UnitPool
from services.intelligence_refresh import mark_dirty_in_transaction

DEMO_DATETIME = datetime.combine(DEMO_DATE, time(8, 0, 0), tzinfo=timezone.utc)

HORIZON_DAYS = int(config.get("optimizer.horizon_days", 3))
MAX_SHIPMENTS = int(config.get("optimizer.max_shipments_per_run", 12))
MAX_SOURCES = int(config.get("optimizer.max_sources_per_deficit", 6))
MAX_ROUTE_PAIRS = int(config.get("optimizer.max_route_pairs_interactive", 120))
MIN_ROUTE_PAIRS_PER_DESTINATION = int(
    config.get("optimizer.min_route_pairs_per_destination", 3)
)
TIME_LIMIT = int(
    (config.get("optimizer.time_limit_seconds") or {}).get("interactive", 30)
)

KEY_COLS = ["facility_id", "component_id", "blood_group_id"]


def load_reference(session):
    facilities = session.scalars(
        select(Facility).where(Facility.is_active.is_(True))
    ).all()
    components = session.scalars(select(Component)).all()
    groups = session.scalars(select(BloodGroup)).all()

    return facilities, components, groups


def load_usable_units(session):
    """Unit-level pool, so the plan can name the physical bags it moves."""

    frame = pd.read_sql(
        select(
            BloodUnit.id,
            BloodUnit.din,
            BloodUnit.facility_id,
            BloodUnit.component_id,
            BloodUnit.blood_group_id,
            BloodUnit.expires_at,
        ).where(
            BloodUnit.status == "AVAILABLE",
            BloodUnit.screening_status == "PASSED",
            BloodUnit.cold_chain_breach_count == 0,
        ),
        session.bind,
    )

    frame["expires_at"] = pd.to_datetime(frame["expires_at"], utc=True)
    frame = frame[frame["expires_at"] > DEMO_DATETIME]

    units_by_series = defaultdict(list)

    for row in frame.itertuples():
        units_by_series[
            (row.facility_id, row.component_id, row.blood_group_id)
        ].append(
            {
                "id": row.id,
                "din": row.din,
                "expires_at": row.expires_at.to_pydatetime(),
            }
        )

    on_hand = {key: len(units) for key, units in units_by_series.items()}

    return units_by_series, on_hand


def load_positions(session):
    """Deficit, surplus and urgency per series over the optimizer horizon."""

    horizon_end = DEMO_DATE + timedelta(days=HORIZON_DAYS)

    frame = pd.read_sql(
        select(
            ShortageRisk.facility_id,
            ShortageRisk.component_id,
            ShortageRisk.blood_group_id,
            ShortageRisk.risk_date,
            ShortageRisk.projected_available,
            ShortageRisk.required_p90,
            ShortageRisk.reserve_floor,
            ShortageRisk.risk_bucket,
            ShortageRisk.shortage_probability,
        ).where(ShortageRisk.risk_date < horizon_end),
        session.bind,
    )

    deficits = {}
    surpluses = {}
    buckets = {}
    reserves = {}

    for key, group in frame.groupby(KEY_COLS):
        gap = (group["required_p90"] - group["projected_available"]).max()
        slack = (group["projected_available"] - group["required_p90"]).min()

        reserves[key] = float(group["reserve_floor"].iloc[0])

        if gap > 0:
            deficits[key] = int(round(gap))
            worst = group.loc[group["shortage_probability"].idxmax()]
            buckets[key] = str(worst["risk_bucket"])
        elif slack > 0:
            surpluses[key] = int(slack)

    return deficits, surpluses, buckets, reserves


def load_at_risk(session):
    """Units the rescue engine says will not be consumed locally in time."""

    rows = session.execute(
        select(
            ExpiryRescue.facility_id,
            ExpiryRescue.component_id,
            ExpiryRescue.blood_group_id,
            ExpiryRescue.rescue_tier,
        ).where(ExpiryRescue.rescue_tier.in_(["ACT_NOW", "WATCH"]))
    ).all()

    at_risk = defaultdict(int)

    for facility_id, component_id, group_id, _ in rows:
        at_risk[(facility_id, component_id, group_id)] += 1

    return dict(at_risk)


def load_compatibility(session, allow_override: bool):
    statement = select(
        Compatibility.component_id,
        Compatibility.recipient_group_id,
        Compatibility.donor_group_id,
        Compatibility.preference_rank,
    ).where(Compatibility.is_compatible.is_(True))

    if not allow_override:
        # ABO-incompatible platelets need an explicit clinical override and must
        # never appear in a routine plan (spec §19.1).
        statement = statement.where(Compatibility.requires_override.is_(False))

    donors_for_recipient = defaultdict(list)

    for component_id, recipient_id, donor_id, rank in session.execute(statement):
        donors_for_recipient[(component_id, recipient_id)].append(
            (donor_id, int(rank or 3))
        )

    return donors_for_recipient


def build_transferable_supply(surpluses, at_risk, on_hand, reserves):
    """How many units a facility may send.

    A unit deep in the FEFO queue will not be consumed before it expires even
    when the facility's three-day requirement exceeds its stock, so at-risk units
    are transferable even where there is no headline surplus. The reserve floor is
    still absolute: it is the constraint that makes this tool acceptable to a
    hospital administrator, and nothing may breach it.
    """

    supply = {}

    for key in set(surpluses) | set(at_risk):
        ceiling = max(0, int(on_hand.get(key, 0) - reserves.get(key, 0.0)))
        candidate = max(int(surpluses.get(key, 0)), int(at_risk.get(key, 0)))

        units = min(candidate, ceiling)

        if units > 0:
            supply[key] = units

    return supply


def supersede_previous_plan(session, replacement_plan_id: str) -> tuple[int, int]:
    """Preserve history while retiring only recommendations never approved."""

    previous_plans = session.scalars(
        select(TransferPlan).where(TransferPlan.status == "GENERATED")
    ).all()
    superseded = session.scalars(
        select(Transfer).where(Transfer.status == "RECOMMENDED")
    ).all()

    for transfer in superseded:
        transfer.status = "SUPERSEDED"

    for previous in previous_plans:
        previous.status = "SUPERSEDED"
        session.add(
            AuditLog(
                id=new_id(),
                created_at=datetime.now(timezone.utc),
                actor="System (optimizer)",
                action="transfer_plan.supersede",
                entity_type="transfer_plan",
                entity_id=previous.id,
                before_json={"status": "GENERATED"},
                after_json={
                    "status": "SUPERSEDED",
                    "replacement_plan_id": replacement_plan_id,
                    "recommendations_superseded": sum(
                        1 for row in superseded if row.plan_id == previous.id
                    ),
                },
            )
        )

    session.flush()
    return len(previous_plans), len(superseded)


def main():
    init_db()
    session = SessionLocal()

    try:
        facilities, components, groups = load_reference(session)

        facilities_by_id = {f.id: f for f in facilities}
        components_by_id = {c.id: c for c in components}
        component_codes = {c.id: c.code for c in components}
        group_codes = {g.id: g.code for g in groups}
        criticality = {
            c.id: float(c.criticality_weight or 1.0) for c in components
        }

        print("Loading unit pool...")
        units_by_series, on_hand = load_usable_units(session)

        print("Loading risk positions...")
        deficits, surpluses, buckets, reserves = load_positions(session)

        print("Loading at-risk units...")
        at_risk = load_at_risk(session)

        supply = build_transferable_supply(surpluses, at_risk, on_hand, reserves)

        print("Building travel matrix...")
        travel_minutes = geo.build_travel_matrix(facilities)
        distances = geo.build_distance_matrix(facilities)

        allow_override = bool(
            config.get("optimizer.allow_override_compatibility", False)
        )
        donors_for_recipient = load_compatibility(session, allow_override)

        storage_headroom = {}

        for facility in facilities:
            for component in components:
                held = sum(
                    count
                    for (f_id, c_id, _), count in on_hand.items()
                    if f_id == facility.id and c_id == component.id
                )
                capacity = policy.storage_capacity(facility, component.code)
                storage_headroom[(facility.id, component.id)] = max(
                    0, int(capacity - held)
                )

        print("Building candidate routes...")
        candidates = build_candidates(
            deficits=deficits,
            surpluses=supply,
            at_risk=at_risk,
            donors_for_recipient=donors_for_recipient,
            travel_minutes=travel_minutes,
            facilities_by_id=facilities_by_id,
            components_by_id=components_by_id,
            departure=DEMO_DATETIME,
            max_sources_per_deficit=MAX_SOURCES,
        )

        opted_in_orgs = set(
            session.scalars(
                select(Organization.id).where(
                    Organization.is_active.is_(True),
                    Organization.network_opt_in.is_(True),
                )
            ).all()
        )

        def sharing_contract(candidate) -> bool:
            source = facilities_by_id.get(candidate["source_id"])
            destination = facilities_by_id.get(candidate["dest_id"])
            if source is None or destination is None:
                return False
            if source.organization_id == destination.organization_id:
                return True
            return bool(
                source.shares_inventory
                and destination.shares_inventory
                and source.organization_id in opted_in_orgs
                and destination.organization_id in opted_in_orgs
            )

        before_consent = len(candidates)
        candidates = [candidate for candidate in candidates if sharing_contract(candidate)]
        excluded_for_consent = before_consent - len(candidates)
        if excluded_for_consent:
            print(
                f"  excluded {excluded_for_consent:,} cross-organization candidates "
                "without active network-sharing consent"
            )
        candidates, shortlist = shortlist_route_pairs(
            candidates=candidates,
            deficits=deficits,
            risk_buckets=buckets,
            criticality=criticality,
            at_risk=at_risk,
            max_pairs=MAX_ROUTE_PAIRS,
            min_pairs_per_destination=MIN_ROUTE_PAIRS_PER_DESTINATION,
        )
        print(f"  candidate routes: {len(candidates):,}")

        print("Solving...")
        weight_setting = session.get(PlatformSetting, "optimizer.weights")
        weight_overrides = (
            dict(weight_setting.value_json or {}) if weight_setting else None
        )
        moves, diagnostics = solve(
            candidates=candidates,
            deficits=deficits,
            surpluses=supply,
            at_risk=at_risk,
            risk_buckets=buckets,
            criticality=criticality,
            storage_headroom=storage_headroom,
            max_shipments=MAX_SHIPMENTS,
            time_limit_seconds=TIME_LIMIT,
            weight_overrides=weight_overrides,
        )

        print("Selecting units FEFO...")
        pool = UnitPool(units_by_series)

        plan_id = new_id()
        transfer_rows = []
        units_assigned = 0

        # Consolidate by (source, destination, component, donor group) so one row
        # is one physical consignment, not one row per recipient group.
        consolidated = defaultdict(
            lambda: {"units": 0, "ranks": [], "recipients": set(), "travel": None}
        )

        for move in moves:
            key = (
                move["source_id"],
                move["dest_id"],
                move["component_id"],
                move["donor_group_id"],
            )
            entry = consolidated[key]
            entry["units"] += move["units"]
            entry["ranks"].append(move["preference_rank"])
            entry["recipients"].add(move["recipient_group_id"])
            entry["travel"] = move["travel_minutes"]

        # Shortest journeys claim units first. A unit with two days left cannot
        # survive a six-hour haul, so if the long haul is served first it takes
        # the fresh units and the near-expiry unit is stranded — sending the
        # freshest bag and keeping the oldest, which is the opposite of FEFO.
        # Serving short trips first sends nearly-expired units somewhere close
        # and fresh units far away.
        for (source_id, dest_id, component_id, donor_group_id), entry in sorted(
            consolidated.items(), key=lambda item: item[1]["travel"]
        ):
            series_key = (source_id, component_id, donor_group_id)
            travel = int(entry["travel"])

            selected = pool.take(
                series_key,
                entry["units"],
                now=DEMO_DATETIME,
                travel_minutes=travel,
            )

            if not selected:
                continue

            units_assigned += len(selected)

            source = facilities_by_id.get(source_id)
            destination = facilities_by_id.get(dest_id)
            component_code = component_codes.get(component_id, "")
            donor_code = group_codes.get(donor_group_id, "")

            recipient_codes = sorted(
                group_codes.get(group_id, "") for group_id in entry["recipients"]
            )
            best_rank = min(entry["ranks"])

            earliest = min(unit["expires_at"] for unit in selected)

            if set(entry["recipients"]) == {donor_group_id}:
                path = f"{donor_code} to {donor_code} (identical, rank 1)"
            else:
                path = (
                    f"{donor_code} to {'/'.join(recipient_codes)} "
                    f"(compatible substitute, rank {best_rank})"
                )

            rationale = (
                f"Move {len(selected)} units of {component_code} {donor_code} "
                f"from {source.name_en if source else source_id} to "
                f"{destination.name_en if destination else dest_id} "
                f"({travel} min). Compatibility path: {path}. "
                f"Units selected first-expiry-first-out; earliest expiry "
                f"{earliest.date().isoformat()}."
            )

            transfer_rows.append(
                {
                    "id": new_id(),
                    "plan_id": plan_id,
                    "from_facility_id": source_id,
                    "to_facility_id": dest_id,
                    "component_id": component_id,
                    "blood_group_id": donor_group_id,
                    "recipient_group_id": (
                        sorted(entry["recipients"])[0] if entry["recipients"] else None
                    ),
                    "preference_rank": best_rank,
                    "units": len(selected),
                    "status": "RECOMMENDED",
                    "unit_ids": [unit["id"] for unit in selected],
                    "est_travel_minutes": travel,
                    "distance_km": round(
                        float(distances.get((source_id, dest_id), 0.0)), 1
                    ),
                    "transport_mode": "ROAD_VALIDATED_BOX",
                    "rationale_en": rationale,
                    "rationale_ur": None,
                    "projected_units_saved": float(len(selected)),
                    "projected_shortage_averted": None,
                    "created_at": DEMO_DATETIME,
                    "recommended_at": DEMO_DATETIME,
                }
            )

        # Impact is measured from what was actually persisted, never from
        # pre-truncation solver values.
        persisted_units = sum(row["units"] for row in transfer_rows)
        persisted_shipments = len(
            {(row["from_facility_id"], row["to_facility_id"]) for row in transfer_rows}
        )

        parameters = {
            "horizon_days": HORIZON_DAYS,
            "max_shipments": MAX_SHIPMENTS,
            "route_pair_shortlist": shortlist,
            "max_route_pairs_interactive": MAX_ROUTE_PAIRS,
            "min_route_pairs_per_destination": MIN_ROUTE_PAIRS_PER_DESTINATION,
            "weights": weights(weight_overrides),
            "allow_override_compatibility": allow_override,
            "solver": diagnostics,
            "plan_as_persisted": {
                "transfers": len(transfer_rows),
                "units": persisted_units,
                "shipments": persisted_shipments,
                "units_assigned_fefo": units_assigned,
            },
        }

        print("Superseding prior unapproved recommendations...")
        # A new solve must never erase an approved or completed chain of
        # custody. Only recommendations that never crossed the human gate are
        # superseded; approved, rejected and executed rows remain immutable
        # history beneath their original plan.
        supersede_previous_plan(session, plan_id)

        session.execute(
            insert(TransferPlan),
            [
                {
                    "id": plan_id,
                    "created_at": DEMO_DATETIME,
                    "plan_type": "ROUTINE",
                    "status": "GENERATED",
                    "scope": "PROVINCE",
                    "parameters_json": parameters,
                    "created_by": "optimizer",
                }
            ],
        )

        if transfer_rows:
            session.execute(insert(Transfer), transfer_rows)

        # Facility KPIs expose pending recommendations, so publish a new
        # decision-snapshot version in the same transaction as the plan.
        mark_dirty_in_transaction(
            session,
            action="optimizer.plan_generated",
            requested_by="system:optimizer",
        )
        session.commit()

        print()
        print(f"Plan {plan_id}")
        print(f"  solver status            {diagnostics['status']}")
        print(f"  wall time                {diagnostics.get('wall_time_seconds')}s")
        gap = diagnostics.get("optimality_gap")
        if gap is not None:
            proven = "proved optimal" if gap <= 1e-6 else "not proved optimal"
            print(f"  optimality gap           {gap:.2%}  ({proven})")
        print(f"  candidate routes         {diagnostics['candidates_considered']:,}")
        print(f"  transfers                {len(transfer_rows)}")
        print(f"  shipments (routes)       {persisted_shipments} / {MAX_SHIPMENTS} allowed")
        print(f"  units moved              {persisted_units}")
        print()
        print(f"  projected deficit        {diagnostics['total_deficit']:,} units")
        print(f"  unmet after plan         {diagnostics['unmet_demand']:,} units")
        print(f"  shortages averted        {diagnostics['shortages_averted']:,} units")
        print(f"  units at risk of expiry  {diagnostics['units_at_risk']:,}")
        print(f"  rescued from expiry      {diagnostics['units_rescued']:,}")

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


if __name__ == "__main__":
    main()
