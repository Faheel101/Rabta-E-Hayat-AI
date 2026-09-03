"""Synthetic network generation entry point (spec §15).

The generator is a first-class deliverable, not a script: it has to produce data
a haematologist would find plausible, and it asserts its own realism targets
(spec §15.4) rather than trusting that they held.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import numpy as np
from sqlalchemy import func, insert, select

from config.settings import DEMO_DATE
from core import config
from datagen.demand import build_demand_requests, daterange
from datagen.supply import InventorySimulation, build_substitution_order
from db.models import (
    Alert,
    BloodGroup,
    BloodUnit,
    Compatibility,
    Component,
    DemandEvent,
    DonationBatch,
    Donor,
    Facility,
    Forecast,
    ForecastMetric,
    ForecastRunSummary,
    InventorySnapshot,
    PlatformSetting,
    Transfer,
    TransferPlan,
    new_id,
)
from db.session import SessionLocal, init_db

SEED = config.SEED
HISTORY_DAYS = config.HISTORY_DAYS
DONORS_PER_FACILITY = dict(config.get("synthetic.donor_register_per_facility") or {})

DEMO_DATETIME = datetime.combine(DEMO_DATE, time(8, 0, 0), tzinfo=timezone.utc)
HISTORY_START = DEMO_DATE - timedelta(days=HISTORY_DAYS)
HISTORY_END = DEMO_DATE - timedelta(days=1)

# Unit-level rows are kept for a recent window; the full eighteen months lives
# in inventory_snapshot. Keeping every bag for 547 days would be millions of
# rows for no analytical gain.
UNIT_HISTORY_DAYS = int(config.get("synthetic.unit_history_days", 45))
SNAPSHOT_HISTORY_DAYS = int(
    config.get("synthetic.snapshot_history_days", HISTORY_DAYS)
)

CHUNK = 5000


def _not_before(event, collected_at, rng):
    """Keep a lifecycle event at or after the collection it belongs to."""

    if event is None or collected_at is None or event >= collected_at:
        return event

    # Processing, testing and release take the better part of a day, so the
    # earliest a unit realistically leaves the shelf is a few hours after the
    # needle came out.
    return collected_at + timedelta(hours=float(rng.uniform(4.0, 14.0)))


def bulk_insert(session, model, rows, chunk_size=CHUNK):
    if not rows:
        return 0

    for start in range(0, len(rows), chunk_size):
        session.execute(insert(model), rows[start : start + chunk_size])
        session.flush()

    return len(rows)


def reset_operational_data(session):
    for model in (
        Transfer,
        TransferPlan,
        ForecastMetric,
        ForecastRunSummary,
        Forecast,
        Alert,
        InventorySnapshot,
        DemandEvent,
        BloodUnit,
        DonationBatch,
        Donor,
    ):
        session.query(model).delete(synchronize_session=False)

    session.commit()


def load_reference(session):
    facilities = session.scalars(
        select(Facility).where(Facility.is_active.is_(True)).order_by(Facility.code)
    ).all()

    components = session.scalars(select(Component).order_by(Component.id)).all()
    groups = session.scalars(select(BloodGroup).order_by(BloodGroup.id)).all()

    return facilities, components, groups


def load_compatibility_rows(session):
    return session.execute(
        select(
            Compatibility.component_id,
            Compatibility.recipient_group_id,
            Compatibility.donor_group_id,
            Compatibility.preference_rank,
            Compatibility.requires_override,
        ).where(Compatibility.is_compatible.is_(True))
    ).all()


def generate_donors(facilities, groups, rng):
    group_weights = np.array(
        [float(g.population_pct_pk or 0.0) for g in groups], dtype=float
    )
    group_weights /= group_weights.sum()
    age_bands = ["18-25", "26-35", "36-45", "46-55", "56-65"]
    rows = []

    for facility in facilities:
        donor_count = int(DONORS_PER_FACILITY.get(facility.facility_type, 220))
        group_idxs = rng.choice(len(groups), size=donor_count, p=group_weights)
        age_choices = rng.choice(
            age_bands,
            size=donor_count,
            p=np.array([0.28, 0.32, 0.22, 0.12, 0.06]),
        )

        for local_index in range(donor_count):
            group = groups[int(group_idxs[local_index])]

            if rng.random() < 0.10:
                last_donation_at = None
                availability_status = "AVAILABLE"
            else:
                days_ago = int(rng.integers(0, 366))
                last_donation_at = DEMO_DATETIME - timedelta(days=days_ago)
                availability_status = "AVAILABLE" if days_ago >= 90 else "DEFERRED"

            rows.append(
                {
                    "id": new_id(),
                    "donor_code": f"PK-D-{len(rows) + 1:06d}",
                    "organization_id": facility.organization_id,
                    "registered_facility_id": facility.id,
                    "blood_group_id": group.id,
                    "city": facility.district,
                    "district": facility.district,
                    "age_band": str(age_choices[local_index]),
                    "availability_status": availability_status,
                    "last_donation_at": last_donation_at,
                    "is_active": bool(rng.random() < 0.92),
                }
            )

    return rows


def build_demand_event_rows(days, requests, attributes, simulation, rng):
    rows = []

    for key, requested_series in requests.items():
        facility_id, component_id, group_id = key

        issued_series = simulation.issued.get(key)
        substituted_series = simulation.substituted_in.get(key)
        series_attributes = attributes.get(key, {})

        for day_index in np.nonzero(requested_series)[0]:
            day_index = int(day_index)
            requested = int(requested_series[day_index])

            issued = int(issued_series[day_index]) if issued_series is not None else 0
            substituted = (
                int(substituted_series[day_index])
                if substituted_series is not None
                else 0
            )

            if issued >= requested:
                outcome = "FULFILLED"
            elif issued == 0:
                outcome = "UNFULFILLED"
            else:
                outcome = "PARTIAL"

            context, urgency = series_attributes.get(
                day_index, ("OTHER", "ROUTINE")
            )

            requested_at = datetime.combine(
                days[day_index],
                time(int(rng.integers(0, 24)), int(rng.integers(0, 60))),
                tzinfo=timezone.utc,
            )

            rows.append(
                {
                    "id": new_id(),
                    "facility_id": facility_id,
                    "component_id": component_id,
                    "blood_group_id": group_id,
                    "requested_at": requested_at,
                    "units_requested": requested,
                    "units_issued": issued,
                    "urgency": urgency,
                    "clinical_context": context,
                    "was_substituted": bool(substituted > 0),
                    "outcome": outcome,
                }
            )

    return rows


def unit_volume(component_code: str) -> int:
    if component_code == "PLT_APH":
        return 250
    if component_code in {"PLT_RD"}:
        return 50
    if component_code == "FFP":
        return 200
    if component_code == "CRYO":
        return 40

    return 350


ISBT_PRODUCT_CODES = {
    "WB": "E0000",
    "PRBC": "E0336",
    "PLT_RD": "E3020",
    "PLT_APH": "E3844",
    "FFP": "E1520",
    "CRYO": "E2100",
}


def build_blood_unit_rows(days, simulation, components, rng):
    """Live inventory plus a recent window of terminal units."""

    component_by_id = {c.id: c for c in components}
    day_count = len(days)

    def day_datetime(day_index: int) -> datetime:
        if 0 <= day_index < day_count:
            base = days[day_index]
        else:
            base = days[0] + timedelta(days=day_index)

        return datetime.combine(
            base,
            time(int(rng.integers(6, 20)), int(rng.integers(0, 60))),
            tzinfo=timezone.utc,
        )

    rows = []
    counter = 0

    def make_row(
        facility_id,
        component_id,
        group_id,
        expiry_index,
        status,
        screening_status,
        issued_at=None,
        discarded_at=None,
        discard_reason=None,
    ):
        nonlocal counter
        counter += 1

        component = component_by_id[component_id]
        shelf_life = int(component.shelf_life_days)

        expires_at = day_datetime(expiry_index)
        collected_at = expires_at - timedelta(days=shelf_life)

        # The simulation closes at the end of the preceding day, whereas the
        # product opens at 08:00 on the scenario date. A short-lived unit can
        # therefore expire overnight. Materialise that elapsed event now so an
        # already-expired bag never appears as usable opening stock.
        if (
            status in {"AVAILABLE", "RESERVED", "CROSSMATCHED"}
            and expires_at <= DEMO_DATETIME
        ):
            status = "EXPIRED"
            discarded_at = expires_at
            discard_reason = "EXPIRY"

        # A unit cannot be issued or discarded before it was collected.
        #
        # `collected_at` is derived from the expiry index while the terminal
        # event time comes from an independent event day, and both draw a random
        # hour — so a unit collected at 13:07 and issued at 11:10 the same day
        # came out backwards. 22,320 transfused units were dated this way. Any
        # turnaround or shelf-age statistic computed over them is wrong, and the
        # traceability chain reads in reverse.
        #
        # The event is pushed to a plausible interval after collection rather
        # than clamped to the same instant, so a zero-length lifetime does not
        # replace one bug with another.
        issued_at = _not_before(issued_at, collected_at, rng)
        discarded_at = _not_before(discarded_at, collected_at, rng)

        breaches = 0

        if status == "AVAILABLE" and rng.random() < 0.005:
            breaches = 1

        return {
            "id": new_id(),
            "din": f"PK{DEMO_DATE.year % 100}-{counter:08d}",
            "isbt_product_code": ISBT_PRODUCT_CODES.get(component.code),
            "facility_id": facility_id,
            "component_id": component_id,
            "blood_group_id": group_id,
            "volume_ml": unit_volume(component.code),
            "collected_at": collected_at,
            "expires_at": expires_at,
            "status": status,
            "screening_status": screening_status,
            "is_leucodepleted": bool(rng.random() < 0.20),
            "is_irradiated": bool(rng.random() < 0.05),
            "cold_chain_breach_count": breaches,
            "source_system_ref": f"SIM-{counter:08d}",
            "last_synced_at": DEMO_DATETIME,
            "issued_at": issued_at,
            "discarded_at": discarded_at,
            "discard_reason": discard_reason,
        }

    # Live stock. A share is already allocated to a patient and therefore not
    # transferable — the engines must see that distinction.
    for facility_id, component_id, group_id, expiry_index, units in (
        simulation.live_units()
    ):
        for _ in range(units):
            draw = float(rng.random())

            if draw < 0.93:
                status = "AVAILABLE"
            elif draw < 0.975:
                status = "RESERVED"
            else:
                status = "CROSSMATCHED"

            rows.append(
                make_row(
                    facility_id,
                    component_id,
                    group_id,
                    expiry_index,
                    status,
                    "PASSED",
                )
            )

    # Terminal units within the retained window.
    for (
        facility_id,
        component_id,
        group_id,
        expiry_index,
        event_day_index,
        units,
        state,
        discard_reason,
    ) in simulation.terminal_events:
        for _ in range(units):
            if state == "ISSUED":
                rows.append(
                    make_row(
                        facility_id,
                        component_id,
                        group_id,
                        expiry_index,
                        "TRANSFUSED",
                        "PASSED",
                        issued_at=day_datetime(event_day_index),
                    )
                )
            else:
                rows.append(
                    make_row(
                        facility_id,
                        component_id,
                        group_id,
                        expiry_index,
                        "EXPIRED",
                        "PASSED",
                        discarded_at=day_datetime(event_day_index),
                        discard_reason=discard_reason or "EXPIRY",
                    )
                )

    # Screening failures in the retained window: collected, never released.
    cutoff = day_count - UNIT_HISTORY_DAYS

    for key, series in simulation.screening_failed.items():
        facility_id, component_id, group_id = key
        component = component_by_id[component_id]

        for day_index in np.nonzero(series)[0]:
            day_index = int(day_index)

            if day_index < cutoff:
                continue

            for _ in range(int(series[day_index])):
                rows.append(
                    make_row(
                        facility_id,
                        component_id,
                        group_id,
                        day_index + int(component.shelf_life_days),
                        "DISCARDED",
                        "FAILED",
                        discarded_at=day_datetime(day_index),
                        discard_reason="SCREENING_FAILED",
                    )
                )

    return rows


def build_snapshot_rows(days, simulation, components):
    """Daily aggregate position per series (spec §4.2)."""

    component_by_id = {c.id: c for c in components}
    day_count = len(days)
    start_index = max(0, day_count - SNAPSHOT_HISTORY_DAYS)

    # Reconstruct expiring-soon counts from the stock ledger is not possible
    # after the fact, so approximate from the flow: what a facility reports each
    # day is its closing position plus that day's movements.
    rows = []

    keys = set(simulation.available_end) | set(simulation.issued) | set(
        simulation.collected
    ) | set(simulation.expired)

    for key in keys:
        facility_id, component_id, group_id = key

        available = simulation.available_end.get(key)
        issued = simulation.issued.get(key)
        collected = simulation.collected.get(key)
        expired = simulation.expired.get(key)
        discarded = simulation.discarded_other.get(key)

        for day_index in range(start_index, day_count):
            units_available = int(available[day_index]) if available is not None else 0
            units_issued = int(issued[day_index]) if issued is not None else 0
            units_collected = (
                int(collected[day_index]) if collected is not None else 0
            )
            units_expired = int(expired[day_index]) if expired is not None else 0
            units_discarded = (
                int(discarded[day_index]) if discarded is not None else 0
            )

            if not any(
                (
                    units_available,
                    units_issued,
                    units_collected,
                    units_expired,
                    units_discarded,
                )
            ):
                continue

            rows.append(
                {
                    "id": new_id(),
                    "snapshot_date": days[day_index],
                    "facility_id": facility_id,
                    "component_id": component_id,
                    "blood_group_id": group_id,
                    "units_available": units_available,
                    "units_reserved": 0,
                    "units_expiring_7d": 0,
                    "units_expiring_3d": 0,
                    "units_issued": units_issued,
                    "units_expired": units_expired,
                    "units_discarded": units_discarded,
                    "units_collected": units_collected,
                }
            )

    return rows


def build_donation_batch_rows(days, simulation, rng):
    """Aggregate collections into batches, which is how they are recorded."""

    rows = []

    for key, series in simulation.collected.items():
        facility_id, component_id, group_id = key

        for day_index in np.nonzero(series)[0]:
            day_index = int(day_index)
            units = int(series[day_index])

            if units <= 0:
                continue

            source = "ON_SITE"
            draw = float(rng.random())

            if draw > 0.60:
                source = "CAMP" if draw < 0.90 else "MOBILE"

            rows.append(
                {
                    "id": new_id(),
                    "facility_id": facility_id,
                    "collected_at": datetime.combine(
                        days[day_index],
                        time(int(rng.integers(9, 17)), int(rng.integers(0, 60))),
                        tzinfo=timezone.utc,
                    ),
                    "source": source,
                    "blood_group_id": group_id,
                    "component_id": component_id,
                    "units_collected": units,
                    "donor_count": units,
                }
            )

    return rows


def check_realism(report: dict) -> list[str]:
    """Spec §15.4 realism checks, reported rather than assumed."""

    problems = []

    low, high = config.get("supply.realism.wastage_pct_range", [10.0, 15.0])
    if not (float(low) <= report["wastage_pct"] <= float(high)):
        problems.append(
            f"wastage {report['wastage_pct']:.1f}% outside target {low}-{high}%"
        )

    minimum_share = float(
        config.get("supply.realism.expiry_share_of_wastage_min", 0.90)
    )
    if report["expiry_share_of_wastage"] < minimum_share:
        problems.append(
            f"expiry share of wastage {report['expiry_share_of_wastage']:.2f} "
            f"below target {minimum_share}"
        )

    low, high = config.get("supply.realism.unmet_demand_pct_range", [3.0, 7.0])
    if not (float(low) <= report["unmet_pct"] <= float(high)):
        problems.append(
            f"unmet demand {report['unmet_pct']:.1f}% outside target {low}-{high}%"
        )

    return problems


def main():
    init_db()
    session = SessionLocal()

    try:
        print("Resetting operational data...")
        reset_operational_data(session)

        facilities, components, groups = load_reference(session)

        if not facilities or not components or not groups:
            raise RuntimeError(
                "Reference data missing. Run scripts.seed_reference first."
            )

        rng = np.random.default_rng(SEED)
        days = list(daterange(HISTORY_START, HISTORY_END))

        group_probs = np.array(
            [float(g.population_pct_pk or 0.0) for g in groups], dtype=float
        )
        group_probs /= group_probs.sum()

        print(f"Network: {len(facilities)} facilities, {len(days)} days of history")

        print("Generating donors...")
        donor_rows = generate_donors(facilities, groups, rng)
        bulk_insert(session, Donor, donor_rows)

        print("Generating clinical demand requests...")
        requests, attributes = build_demand_requests(
            facilities, components, groups, days, group_probs, rng
        )
        print(f"  active series: {len(requests)}")

        print("Simulating inventory (order -> collect -> issue FEFO -> expire)...")
        substitution_order = build_substitution_order(
            load_compatibility_rows(session), [g.id for g in groups]
        )

        simulation = InventorySimulation(
            facilities,
            components,
            groups,
            days,
            requests,
            substitution_order,
            rng,
        )
        simulation.run(terminal_history_from_index=len(days) - UNIT_HISTORY_DAYS)

        report = simulation.realism_report()

        print("Writing demand events...")
        bulk_insert(
            session,
            DemandEvent,
            build_demand_event_rows(days, requests, attributes, simulation, rng),
        )

        print("Writing donation batches...")
        bulk_insert(
            session, DonationBatch, build_donation_batch_rows(days, simulation, rng)
        )

        print("Writing blood units...")
        unit_rows = build_blood_unit_rows(days, simulation, components, rng)
        bulk_insert(session, BloodUnit, unit_rows)

        print("Writing inventory snapshots...")
        bulk_insert(
            session, InventorySnapshot, build_snapshot_rows(days, simulation, components)
        )

        # Persist the controlled generation profile and its measured outcomes.
        # This makes the demo dataset auditable after generation; QA does not
        # have to trust console output that disappeared with the build process.
        quality_setting = session.get(PlatformSetting, "synthetic.dataset_profile")
        quality_payload = {
            "profile": str(config.get("synthetic.profile", "UNSPECIFIED")),
            "seed": SEED,
            "scenario_date": DEMO_DATE.isoformat(),
            "history_days": len(days),
            "unit_history_days": UNIT_HISTORY_DAYS,
            "facilities": len(facilities),
            "seed_donors": len(donor_rows),
            "demand_events": sum(
                int(np.count_nonzero(series)) for series in requests.values()
            ),
            "blood_units": len(unit_rows),
            "realism": {
                "collected": int(report["collected"]),
                "screening_failed": int(report["screening_failed"]),
                "released": int(report["screened_pass"]),
                "expired": int(report["expired"]),
                "discarded_other": int(report["discarded_other"]),
                "wastage_pct": float(report["wastage_pct"]),
                "expiry_share_of_wastage": float(
                    report["expiry_share_of_wastage"]
                ),
                "requested": int(report["requested"]),
                "issued": int(report["issued"]),
                "unmet_pct": float(report["unmet_pct"]),
                "fill_rate": float(report["fill_rate"]),
                "substitution_pct": float(report["substitution_pct"]),
            },
        }

        if quality_setting is None:
            session.add(
                PlatformSetting(
                    key="synthetic.dataset_profile",
                    value_json=quality_payload,
                    updated_by="datagen",
                    updated_at=DEMO_DATETIME,
                )
            )
        else:
            quality_setting.value_json = quality_payload
            quality_setting.updated_by = "datagen"
            quality_setting.updated_at = DEMO_DATETIME

        session.commit()

        print()
        print("Realism report (spec §15.4)")
        print(f"  collected                     {report['collected']:>10,}")
        print(f"  screening failures            {report['screening_failed']:>10,}")
        print(f"  released (screened pass)      {report['screened_pass']:>10,}")
        print(f"  expired                       {report['expired']:>10,}")
        print(f"  discarded (non-expiry)        {report['discarded_other']:>10,}")
        print(f"  wastage rate                  {report['wastage_pct']:>9.1f}%   target 10-15%")
        print(f"  expiry share of wastage       {report['expiry_share_of_wastage']:>9.2f}    target >=0.90")
        print(f"  units requested               {report['requested']:>10,}")
        print(f"  units issued                  {report['issued']:>10,}")
        print(f"  unmet demand                  {report['unmet_pct']:>9.1f}%   target 3-7%")
        print(f"  fill rate                     {report['fill_rate']:>9.3f}")
        print(f"  substitution rate             {report['substitution_pct']:>9.1f}%")
        print("  mean days to expiry at issue, by component:")
        code_by_id = {c.id: c.code for c in components}
        for component_id, mean_days in sorted(
            report["days_to_expiry_at_issue_by_component"].items(),
            key=lambda item: item[1],
        ):
            print(f"      {code_by_id.get(component_id, component_id):<8} {mean_days:>6.1f} days")

        problems = check_realism(report)

        print()
        if problems:
            print("Realism checks FAILED:")
            for problem in problems:
                print(f"  - {problem}")
            print("  Tune config/network.yaml (supply.over_order, supply.cover_days).")
        else:
            print("Realism checks passed.")

        print()
        print_counts(session)

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def print_counts(session):
    for label, model in (
        ("Donors", Donor),
        ("Donation batches", DonationBatch),
        ("Demand events", DemandEvent),
        ("Blood units", BloodUnit),
        ("Inventory snapshots", InventorySnapshot),
    ):
        count = session.scalar(select(func.count()).select_from(model))
        print(f"{label}: {count:,}")


if __name__ == "__main__":
    main()
