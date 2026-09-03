"""Validate the rebuilt demonstration dataset and its facility distribution.

The generator already checks global clinical realism while it is running. This
command verifies that those measured outcomes were persisted and that the final
post-pipeline database remains coherent and sensibly distributed by facility.

    python -m scripts.validate_synthetic_dataset
    python -m scripts.validate_synthetic_dataset --database /tmp/rabta.db --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote

import yaml

from config.settings import DATABASE_URL, DEMO_DATE


@dataclass(frozen=True)
class FacilityProfile:
    code: str
    facility_type: str
    donors: int
    live_units: int
    demand_events_30d: int
    requested_units_30d: int
    capacity: int


def _default_database_path() -> Path:
    prefix = "sqlite:///"
    if not DATABASE_URL.startswith(prefix):
        raise SystemExit("--database is required when DATABASE_URL is not SQLite")
    return Path(unquote(DATABASE_URL[len(prefix) :])).expanduser().resolve()


def _scalar(connection: sqlite3.Connection, query: str, parameters=()) -> int:
    row = connection.execute(query, parameters).fetchone()
    return int(row[0] or 0)


def _capacity(raw: str | None) -> int:
    if not raw:
        return 0
    values = json.loads(raw)
    return sum(max(0, int(value)) for value in values.values())


def validate(database: Path) -> dict:
    config_path = Path(__file__).resolve().parents[1] / "config" / "network.yaml"
    network = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    targets = network["synthetic"]["donor_register_per_facility"]
    realism_targets = network["supply"]["realism"]
    scenario_now = f"{DEMO_DATE.isoformat()} 08:00:00"
    recent_start = (DEMO_DATE - timedelta(days=29)).isoformat()

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row

    try:
        profile_row = connection.execute(
            "SELECT value_json FROM platform_setting WHERE key = ?",
            ("synthetic.dataset_profile",),
        ).fetchone()
        profile = json.loads(profile_row[0]) if profile_row else {}
        realism = profile.get("realism", {})

        rows = connection.execute(
            """
            WITH donor_counts AS (
                SELECT registered_facility_id AS facility_id, COUNT(*) AS donors
                FROM donor GROUP BY registered_facility_id
            ), live_stock AS (
                SELECT facility_id, COUNT(*) AS live_units
                FROM blood_unit
                WHERE status IN ('AVAILABLE', 'RESERVED', 'CROSSMATCHED')
                  AND screening_status = 'PASSED'
                  AND expires_at > ?
                GROUP BY facility_id
            ), recent_demand AS (
                SELECT facility_id, COUNT(*) AS demand_events,
                       SUM(units_requested) AS requested_units
                FROM demand_event
                WHERE requested_at >= ? AND requested_at <= ?
                GROUP BY facility_id
            )
            SELECT f.code, f.facility_type, f.storage_capacity_json,
                   COALESCE(d.donors, 0) AS donors,
                   COALESCE(s.live_units, 0) AS live_units,
                   COALESCE(r.demand_events, 0) AS demand_events,
                   COALESCE(r.requested_units, 0) AS requested_units
            FROM facility f
            LEFT JOIN donor_counts d ON d.facility_id = f.id
            LEFT JOIN live_stock s ON s.facility_id = f.id
            LEFT JOIN recent_demand r ON r.facility_id = f.id
            WHERE f.is_active = 1
            ORDER BY f.facility_type, f.code
            """,
            (scenario_now, recent_start, scenario_now),
        ).fetchall()

        facilities = [
            FacilityProfile(
                code=row["code"],
                facility_type=row["facility_type"],
                donors=int(row["donors"]),
                live_units=int(row["live_units"]),
                demand_events_30d=int(row["demand_events"]),
                requested_units_30d=int(row["requested_units"]),
                capacity=_capacity(row["storage_capacity_json"]),
            )
            for row in rows
        ]

        failures: list[str] = []

        if profile.get("profile") != network["synthetic"]["profile"]:
            failures.append("persisted synthetic profile is missing or stale")

        waste_low, waste_high = realism_targets["wastage_pct_range"]
        unmet_low, unmet_high = realism_targets["unmet_demand_pct_range"]
        expiry_min = realism_targets["expiry_share_of_wastage_min"]
        wastage = float(realism.get("wastage_pct", -1))
        unmet = float(realism.get("unmet_pct", -1))
        expiry_share = float(realism.get("expiry_share_of_wastage", -1))

        if not float(waste_low) <= wastage <= float(waste_high):
            failures.append(f"wastage {wastage:.1f}% is outside {waste_low}-{waste_high}%")
        if not float(unmet_low) <= unmet <= float(unmet_high):
            failures.append(f"unmet demand {unmet:.1f}% is outside {unmet_low}-{unmet_high}%")
        if expiry_share < float(expiry_min):
            failures.append(
                f"expiry share {expiry_share:.2f} is below {float(expiry_min):.2f}"
            )

        for facility in facilities:
            minimum = int(targets.get(facility.facility_type, 180))
            if facility.donors < minimum or facility.donors > minimum * 3:
                failures.append(
                    f"{facility.code}: {facility.donors} donors outside "
                    f"expected {minimum}-{minimum * 3}"
                )
            if facility.live_units <= 0:
                failures.append(f"{facility.code}: no usable live inventory")
            if facility.capacity and facility.live_units > facility.capacity:
                failures.append(
                    f"{facility.code}: {facility.live_units} live units exceed "
                    f"configured capacity {facility.capacity}"
                )
            if facility.demand_events_30d <= 0:
                failures.append(f"{facility.code}: no demand in the last 30 days")

        consistency = {
            "foreign_key_violations": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
            "donors_without_scope": _scalar(
                connection,
                """SELECT COUNT(*) FROM donor
                   WHERE organization_id IS NULL OR registered_facility_id IS NULL""",
            ),
            "donor_scope_mismatches": _scalar(
                connection,
                """SELECT COUNT(*) FROM donor d JOIN facility f
                     ON f.id = d.registered_facility_id
                   WHERE d.organization_id != f.organization_id""",
            ),
            "invalid_active_units": _scalar(
                connection,
                """SELECT COUNT(*) FROM blood_unit
                   WHERE status IN ('AVAILABLE', 'RESERVED', 'CROSSMATCHED')
                     AND (screening_status != 'PASSED' OR expires_at <= ?)""",
                (scenario_now,),
            ),
            "invalid_unit_lifecycle": _scalar(
                connection,
                """SELECT COUNT(*) FROM blood_unit
                   WHERE expires_at <= collected_at
                      OR (issued_at IS NOT NULL AND issued_at < collected_at)
                      OR (discarded_at IS NOT NULL AND discarded_at < collected_at)""",
            ),
            "invalid_demand_outcomes": _scalar(
                connection,
                """SELECT COUNT(*) FROM demand_event
                   WHERE units_requested <= 0 OR units_issued < 0
                      OR units_issued > units_requested""",
            ),
        }

        for label, count in consistency.items():
            if count:
                failures.append(f"{label.replace('_', ' ')}: {count}")

        return {
            "database": str(database),
            "profile": profile,
            "facilities": [asdict(row) for row in facilities],
            "consistency": consistency,
            "passed": not failures,
            "failures": failures,
        }
    finally:
        connection.close()


def _print_report(report: dict) -> None:
    profile = report["profile"]
    realism = profile.get("realism", {})
    print(f"Synthetic profile: {profile.get('profile', 'MISSING')}")
    print(
        "Realism: "
        f"wastage {float(realism.get('wastage_pct', 0)):.1f}% · "
        f"unmet {float(realism.get('unmet_pct', 0)):.1f}% · "
        f"fill {float(realism.get('fill_rate', 0)):.3f} · "
        f"expiry share {float(realism.get('expiry_share_of_wastage', 0)):.2f}"
    )
    print()
    print("Facility            Type                Donors  Live  Demand 30d  Capacity")
    for row in report["facilities"]:
        print(
            f"{row['code']:<19} {row['facility_type']:<19} "
            f"{row['donors']:>6} {row['live_units']:>6} "
            f"{row['demand_events_30d']:>11} {row['capacity']:>9}"
        )
    print()
    if report["passed"]:
        print("Dataset validation PASSED")
    else:
        print("Dataset validation FAILED")
        for failure in report["failures"]:
            print(f"  - {failure}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=_default_database_path())
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    report = validate(args.database.expanduser().resolve())
    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
