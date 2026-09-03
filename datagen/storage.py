"""Storage locations and their temperature history.

    python -m datagen.storage

`storage_location` and `temperature_log` have been in the schema since it was
written and both are empty, while `blood_unit.cold_chain_breach_count` is set on
118 units by the generator with nothing behind it. A breach count with no
recorded excursion is a number nobody can investigate: you cannot ask which
fridge, when, how warm, or which other units were in it.

This gives every facility the storage it must have to hold what it holds, logs a
reading every half hour, and puts real excursions in — door left open, compressor
failing overnight, a power cut. Units are placed by component, because a platelet
in a blood bank fridge is a destroyed platelet.

Temperatures come from `config/network.yaml` rather than from here, so a
transfusion specialist can review them without reading Python.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import numpy as np
from sqlalchemy import bindparam, delete, func, select

from core import config
from core.clock import DEMO_DATETIME
from db.models import BloodUnit, Component, Facility, StorageLocation, TemperatureLog
from db.session import SessionLocal, init_db

SEED = config.SEED

# How long a history of readings to write. Long enough that an excursion is
# visible in a chart and short enough that the table stays a sane size.
HISTORY_DAYS = 14
READING_INTERVAL_MINUTES = 30

# Which store each component belongs in. Getting this wrong is not a filing
# error: a platelet held at 4°C loses function, and red cells frozen without
# cryoprotectant haemolyse.
COMPONENT_STORE = {
    "PRBC": "BLOOD_BANK_FRIDGE",
    "WB": "BLOOD_BANK_FRIDGE",
    "FFP": "PLASMA_FREEZER",
    "CRYO": "PLASMA_FREEZER",
    "PLT_RD": "PLATELET_AGITATOR",
    "PLT_APH": "PLATELET_AGITATOR",
}

# Where untested stock goes instead. Same temperature, different door.
# PLATELET_AGITATOR is deliberately absent: platelets have no quarantine store,
# and a missing entry here leaves them where they are.
QUARANTINE_STORE = {
    "BLOOD_BANK_FRIDGE": "QUARANTINE_FRIDGE",
    "PLASMA_FREEZER": "QUARANTINE_FREEZER",
}


def store_types() -> dict[str, dict]:
    """Store definitions from config, so the ranges are reviewable."""

    return dict(config.get("storage.location_types") or {})


def _excursion_plan(rng, days: int) -> list[dict]:
    """When this store misbehaved, and how.

    Three shapes, because they have different consequences. A door left open
    warms fast and recovers fast; a failing compressor drifts for hours; a power
    cut is total until somebody notices.

    The duration distributions matter more than the rates. Drawing door events
    uniformly over half an hour to two hours made every single one of them clear
    the 30-minute materiality threshold, which flagged nineteen per cent of the
    estate as untransferable — not a busy blood bank, a broken one. Door events
    are overwhelmingly short: somebody takes a bag out and shuts it. The long
    tail is real but rare, and it is the tail that should trip the threshold.

    So the estate this produces has minor blips almost everywhere and a genuine
    problem in a handful of stores, which is what a fortnight in a working
    network looks like.
    """

    plan = []

    if rng.random() < 0.45:
        plan.append(
            {
                "kind": "DOOR_LEFT_OPEN",
                "start_hours_ago": float(rng.uniform(6, days * 24)),
                # Mean around 12 minutes, with a thin tail reaching an hour or
                # more for the ones somebody genuinely forgot.
                "duration_hours": float(min(2.0, rng.exponential(0.2) + 0.05)),
                "delta_c": float(rng.uniform(3.0, 7.0)),
            }
        )

    if rng.random() < 0.06:
        plan.append(
            {
                "kind": "COMPRESSOR_DRIFT",
                "start_hours_ago": float(rng.uniform(12, days * 24)),
                "duration_hours": float(rng.uniform(4.0, 14.0)),
                "delta_c": float(rng.uniform(2.0, 5.0)),
            }
        )

    if rng.random() < 0.025:
        plan.append(
            {
                "kind": "POWER_INTERRUPTION",
                "start_hours_ago": float(rng.uniform(24, days * 24)),
                "duration_hours": float(rng.uniform(1.0, 6.0)),
                "delta_c": float(rng.uniform(6.0, 14.0)),
            }
        )

    return plan


def build_locations(session, rng) -> list[dict]:
    """One store of each needed type per facility, sized to what it holds."""

    definitions = store_types()
    facilities = session.scalars(select(Facility)).all()

    held = dict(
        session.execute(
            select(BloodUnit.facility_id, func.count())
            .where(BloodUnit.status.in_(("AVAILABLE", "RESERVED", "QUARANTINE")))
            .group_by(BloodUnit.facility_id)
        ).all()
    )

    rows = []

    for facility in facilities:
        on_hand = held.get(facility.id, 0)

        for type_code, spec in definitions.items():
            # A big centre runs several fridges; a THQ runs one of each.
            count = 1

            if type_code == "BLOOD_BANK_FRIDGE" and on_hand > 800:
                count = 2 if on_hand < 2500 else 3

            for index in range(1, count + 1):
                suffix = f"-{index}" if count > 1 else ""

                rows.append(
                    {
                        "id": str(uuid.uuid4()),
                        "facility_id": facility.id,
                        "code": f"{facility.code}-{spec['short']}{suffix}",
                        "name": f"{spec['name']}{' ' + str(index) if count > 1 else ''}",
                        "location_type": type_code,
                        "target_temp_min_c": float(spec["min_c"]),
                        "target_temp_max_c": float(spec["max_c"]),
                        "capacity_units": int(spec.get("capacity_units") or 400),
                        "is_quarantine": bool(spec.get("is_quarantine", False)),
                        "has_agitator": bool(spec.get("has_agitator", False)),
                        "is_out_of_range": False,
                        "is_active": True,
                    }
                )

    return rows


def build_readings(rng, locations: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """A reading every half hour, with excursions where they happened."""

    readings = []
    latest: dict[str, dict] = {}
    steps = int(HISTORY_DAYS * 24 * 60 / READING_INTERVAL_MINUTES)

    for location in locations:
        low = location["target_temp_min_c"]
        high = location["target_temp_max_c"]
        midpoint = (low + high) / 2.0
        # Normal variation is a fraction of the band, not the whole of it — a
        # store that wanders the full range is one about to breach.
        noise = (high - low) / 8.0

        plan = _excursion_plan(rng, HISTORY_DAYS)

        for step in range(steps):
            hours_ago = (steps - step) * READING_INTERVAL_MINUTES / 60.0
            recorded_at = DEMO_DATETIME - timedelta(hours=hours_ago)

            temperature = float(rng.normal(midpoint, noise))
            source = "PROBE"

            for excursion in plan:
                start = excursion["start_hours_ago"]
                end = start - excursion["duration_hours"]

                if end <= hours_ago <= start:
                    # Ramp in and out rather than a step: a fridge warms and
                    # recovers gradually, and a square wave in the chart would
                    # be obviously synthetic.
                    progress = (start - hours_ago) / max(
                        1e-6, excursion["duration_hours"]
                    )
                    shape = np.sin(np.pi * min(1.0, max(0.0, progress)))
                    temperature += excursion["delta_c"] * float(shape)
                    source = excursion["kind"]

            out_of_range = temperature < low or temperature > high

            readings.append(
                {
                    "id": str(uuid.uuid4()),
                    "storage_location_id": location["id"],
                    "recorded_at": recorded_at,
                    "temperature_c": round(temperature, 2),
                    "is_out_of_range": out_of_range,
                    "source": source,
                    "recorded_by": None,
                    "action_taken": (
                        "Alarm acknowledged; contents assessed."
                        if out_of_range
                        else None
                    ),
                }
            )

            latest[location["id"]] = {
                "temperature_c": round(temperature, 2),
                "recorded_at": recorded_at,
                "is_out_of_range": out_of_range,
            }

    return readings, latest


def place_units(session, locations: list[dict]) -> list[dict]:
    """Put every unit in a store that can actually hold it."""

    by_facility_type: dict[tuple[str, str], list[str]] = {}

    for location in locations:
        key = (location["facility_id"], location["location_type"])
        by_facility_type.setdefault(key, []).append(location["id"])

    component_code = {
        row.id: row.code for row in session.scalars(select(Component)).all()
    }

    units = session.execute(
        select(
            BloodUnit.id,
            BloodUnit.facility_id,
            BloodUnit.component_id,
            BloodUnit.status,
        ).where(
            BloodUnit.status.in_(
                ("AVAILABLE", "RESERVED", "CROSSMATCHED", "QUARANTINE")
            )
        )
    ).all()

    placements = []
    cursor: dict[tuple, int] = {}

    for unit in units:
        code = component_code.get(unit.component_id)
        store_type = COMPONENT_STORE.get(code, "BLOOD_BANK_FRIDGE")

        # Untested stock lives physically apart. That separation is the whole
        # reason quarantine storage exists — so an untested bag cannot be taken
        # off the issuable shelf by somebody in a hurry.
        #
        # Segregation moves a unit sideways at the same temperature, never up or
        # down it. Sending untested plasma to the quarantine FRIDGE to segregate
        # it would thaw 291 units — the segregation costing more than the risk it
        # removes. Platelets have no quarantine store at all and are the one
        # documented exception; see the config note.
        if unit.status == "QUARANTINE":
            store_type = QUARANTINE_STORE.get(store_type, store_type)

        key = (unit.facility_id, store_type)
        candidates = by_facility_type.get(key)

        if not candidates:
            continue

        # Round-robin across a facility's stores of that type, so a bank with
        # three fridges fills them evenly rather than overloading the first.
        index = cursor.get(key, 0)
        cursor[key] = index + 1

        placements.append(
            {"b_id": unit.id, "storage_location_id": candidates[index % len(candidates)]}
        )

    return placements


def recompute_breaches(session) -> tuple[int, int]:
    """Derive each unit's cold-chain breach count from what actually happened.

    `blood_unit.cold_chain_breach_count` was set on 118 units by the generator
    with nothing behind it — 87 of them in stores that never went out of range,
    while 8,336 units that genuinely sat through an excursion read zero. The
    number drives a badge on the stock list and blocks transfers, so it has to
    come from the temperature log rather than from a random draw.

    Not every out-of-range reading counts. A door held open for one minute is
    not a breach, and treating it as one would condemn most of the shelf and
    make the flag worthless. The materiality thresholds are in config where a
    transfusion specialist can review them.
    """

    thresholds = config.get("storage.breach_thresholds") or {}
    max_minutes = float(thresholds.get("max_minutes", 30))
    max_excess = float(thresholds.get("max_excess_c", 4.0))

    # Group each store's out-of-range readings into events, the same way the
    # store detail page does: consecutive readings are one thing that happened.
    events: dict[str, list[tuple]] = {}

    rows = session.execute(
        select(
            TemperatureLog.storage_location_id,
            TemperatureLog.recorded_at,
            TemperatureLog.temperature_c,
            TemperatureLog.is_out_of_range,
            StorageLocation.target_temp_min_c,
            StorageLocation.target_temp_max_c,
        )
        .join(StorageLocation, StorageLocation.id == TemperatureLog.storage_location_id)
        .order_by(TemperatureLog.storage_location_id, TemperatureLog.recorded_at)
    ).all()

    current: dict | None = None
    current_store: str | None = None

    def close(store: str, event: dict) -> None:
        minutes = (event["end"] - event["start"]).total_seconds() / 60.0
        # A lone reading stands for the interval it was sampled over.
        minutes = max(float(READING_INTERVAL_MINUTES), minutes)

        if minutes > max_minutes or event["excess"] > max_excess:
            events.setdefault(store, []).append((event["start"], event["end"]))

    for row in rows:
        if row.is_out_of_range:
            excess = max(
                float(row.temperature_c) - float(row.target_temp_max_c),
                float(row.target_temp_min_c) - float(row.temperature_c),
            )

            if current is None or current_store != row.storage_location_id:
                if current is not None:
                    close(current_store, current)

                current_store = row.storage_location_id
                current = {
                    "start": row.recorded_at,
                    "end": row.recorded_at,
                    "excess": excess,
                }
            else:
                current["end"] = row.recorded_at
                current["excess"] = max(current["excess"], excess)

        elif current is not None:
            close(current_store, current)
            current = None
            current_store = None

    if current is not None:
        close(current_store, current)

    # Every unit starts at zero. A stale count on a unit whose store turned out
    # to be clean is exactly the error being corrected.
    session.execute(BloodUnit.__table__.update().values(cold_chain_breach_count=0))

    units = session.execute(
        select(BloodUnit.id, BloodUnit.storage_location_id, BloodUnit.collected_at)
        .where(BloodUnit.storage_location_id.is_not(None))
    ).all()

    updates = []

    for unit in units:
        spans = events.get(unit.storage_location_id)

        if not spans:
            continue

        # Only excursions since the unit was placed. A fridge that failed before
        # this bag arrived says nothing about this bag.
        count = sum(1 for start, end in spans if end >= (unit.collected_at or start))

        if count:
            updates.append({"b_id": unit.id, "cold_chain_breach_count": count})

    for start in range(0, len(updates), 10000):
        session.connection().execute(
            BloodUnit.__table__.update()
            .where(BloodUnit.id == bindparam("b_id"))
            .values(cold_chain_breach_count=bindparam("cold_chain_breach_count")),
            updates[start : start + 10000],
        )

    return len(updates), sum(len(v) for v in events.values())


def main() -> None:
    init_db()
    session = SessionLocal()
    rng = np.random.default_rng(SEED)

    try:
        print("Clearing previous storage data...")
        session.execute(
            BloodUnit.__table__.update().values(storage_location_id=None)
        )
        session.execute(delete(TemperatureLog))
        session.execute(delete(StorageLocation))
        session.flush()

        locations = build_locations(session, rng)
        print(f"Storage locations: {len(locations):,}")

        session.bulk_insert_mappings(StorageLocation, locations)
        session.flush()

        readings, latest = build_readings(rng, locations)
        print(f"Temperature readings: {len(readings):,}"
              f"  ({HISTORY_DAYS} days at {READING_INTERVAL_MINUTES} minute intervals)")

        for start in range(0, len(readings), 10000):
            session.bulk_insert_mappings(
                TemperatureLog, readings[start : start + 10000]
            )

        # The store's own last-reading fields, so a page does not have to scan
        # the log to say whether a fridge is behaving.
        for location in locations:
            state = latest.get(location["id"])

            if not state:
                continue

            session.execute(
                StorageLocation.__table__.update()
                .where(StorageLocation.id == location["id"])
                .values(
                    last_temp_c=state["temperature_c"],
                    last_temp_at=state["recorded_at"],
                    is_out_of_range=state["is_out_of_range"],
                )
            )

        placements = place_units(session, locations)
        print(f"Units placed: {len(placements):,}")

        for start in range(0, len(placements), 10000):
            session.connection().execute(
                BloodUnit.__table__.update()
                .where(BloodUnit.id == bindparam("b_id"))
                .values(storage_location_id=bindparam("storage_location_id")),
                placements[start : start + 10000],
            )

        flagged, material = recompute_breaches(session)
        print(
            f"Cold-chain breaches: {material:,} material excursions, "
            f"{flagged:,} units flagged"
        )

        session.commit()

        excursions = session.scalar(
            select(func.count())
            .select_from(TemperatureLog)
            .where(TemperatureLog.is_out_of_range.is_(True))
        )
        breached_stores = session.scalar(
            select(func.count(func.distinct(TemperatureLog.storage_location_id))).where(
                TemperatureLog.is_out_of_range.is_(True)
            )
        )

        print()
        print(f"Out-of-range readings: {excursions:,}"
              f" across {breached_stores} stores")
        print()
        print("Every excursion is now answerable: which store, when, how warm,")
        print("and which units were in it at the time.")

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


if __name__ == "__main__":
    main()
