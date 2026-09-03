"""Reissue blood unit identifiers in the ISBT 128 Data Structure 001 layout.

    python -m scripts.migrate_dins --dry-run
    python -m scripts.migrate_dins

The previous scheme (PK26-00014823) is not ISBT 128. This rewrites every unit to
the verified 13-character layout — 5-character Facility Identification Number,
2-digit year, 6-digit sequence — each carrying a valid ISO/IEC 7064 MOD 37-2
check character.

WHAT THIS DOES NOT DO: make the identifiers conformant. A real DIN needs an
ICCBBA-assigned FIN, which cannot be self-allocated; inventing one risks
colliding with a real foreign facility and breaking the global uniqueness the
standard exists to guarantee. Each facility is therefore given a provisional FIN
in the Z-block, and everything downstream reports `is_provisional`. Registering
with ICCBBA later means setting `labelling.isbt128.fin` and re-running this.

Sequence numbers are allocated per (FIN, year), which is the only uniqueness
requirement the standard imposes.
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from sqlalchemy import func, select

from core import isbt
from core.clock import as_utc
from db.models import BloodUnit, Facility
from db.session import SessionLocal, init_db


def provisional_fin(index: int) -> str:
    """A structurally valid but deliberately un-assignable FIN.

    'Z' is used because ICCBBA has not allocated Z-prefixed FINs to any facility
    in this network, so a provisional identifier cannot be mistaken for a real
    one — and the code never claims otherwise.
    """

    return f"Z{index:04d}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Reissue DINs in ISBT 128 layout.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    init_db()
    session = SessionLocal()

    try:
        facilities = session.scalars(
            select(Facility).order_by(Facility.code)
        ).all()

        fin_for_facility = {
            facility.id: provisional_fin(index + 1)
            for index, facility in enumerate(facilities)
        }

        # A configured FIN means ICCBBA has assigned one; use it and drop the
        # provisional flag. Otherwise every facility gets a Z-block placeholder.
        assigned_fin, is_provisional = isbt.configured_fin()

        if not is_provisional:
            print(f"Using the ICCBBA-assigned FIN {assigned_fin}.")
            fin_for_facility = {facility.id: assigned_fin for facility in facilities}

        units = session.scalars(
            select(BloodUnit).order_by(BloodUnit.collected_at, BloodUnit.din)
        ).all()

        print(f"Units to reissue: {len(units):,}")

        # Sequence counters per (FIN, year) — the standard's only uniqueness rule.
        counters: dict[tuple[str, int], int] = defaultdict(int)
        seen: set[str] = set()

        changes = []
        skipped = 0

        for unit in units:
            collected = as_utc(unit.collected_at)

            if collected is None:
                skipped += 1
                continue

            fin = fin_for_facility.get(unit.facility_id)

            if fin is None:
                skipped += 1
                continue

            year = collected.year
            counters[(fin, year)] += 1
            sequence = counters[(fin, year)]

            if sequence > 999999:
                raise RuntimeError(
                    f"{fin} exhausted the 6-digit sequence for {year}; a real "
                    "facility would allocate a second FIN"
                )

            identifier = isbt.build_din(
                sequence=sequence, year=year, fin=fin, provisional=is_provisional
            )

            if identifier.din in seen:
                raise RuntimeError(f"duplicate DIN generated: {identifier.din}")

            seen.add(identifier.din)
            changes.append((unit, identifier))

        print(f"  reissuing {len(changes):,}, skipping {skipped:,}")

        if changes:
            first_unit, first_id = changes[0]
            print()
            print("Example:")
            print(f"  before  {first_unit.din}")
            print(f"  after   {first_id.din}  check [{first_id.check_character}]")
            print(f"  barcode {first_id.barcode_content}")
            print(f"  FIN {first_id.fin} · year {first_id.year} · "
                  f"sequence {first_id.sequence}")
            print(f"  provisional: {first_id.is_provisional}")

        if args.dry_run:
            print()
            print("Dry run; nothing written.")
            return 0

        for unit, identifier in changes:
            unit.din = identifier.din

        session.commit()

        total = session.scalar(select(func.count()).select_from(BloodUnit))
        distinct = session.scalar(
            select(func.count(func.distinct(BloodUnit.din))).select_from(BloodUnit)
        )

        print()
        print(f"Reissued. {total:,} units, {distinct:,} distinct DINs.")

        if total != distinct:
            raise RuntimeError("DIN collision after migration")

        status = isbt.conformance_status()
        print()
        print(status["message"])

        return 0

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
