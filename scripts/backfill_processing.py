"""Give every existing unit a separation record explaining where it came from.

    python -m scripts.backfill_processing --dry-run
    python -m scripts.backfill_processing

Unit creation moved from collection to processing (#26). The 109,311 units the
generator made before that change have no separation behind them, so the
traceability chain has a cliff edge in it: ask "who spun this bag and what did it
yield" and everything before today answers nothing.

This reconstructs the answer from what the units themselves show, and is careful
about what it does NOT know.

`units_produced` is what actually exists. `units_expected` is set EQUAL to it,
because the expected recipe was never captured for these separations — the field
did not exist. Inferring the expectation from the bag type and calling the
difference a loss would have written a 79% shortfall rate into the yield report
as though somebody had measured it. Every one of those "losses" was an artifact
of reconstruction.

So these records say what was made and decline to guess what was intended.
`services.processing.yield_summary` excludes them, and only separations recorded
through the processing module count toward a measured yield.

Separation time is placed between collection and the earliest evidence the units
were in use.
"""

from __future__ import annotations

import argparse
import uuid
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, select

from core.clock import DEMO_DATETIME, as_utc
from db.models import BloodUnit, Component, ComponentProduction, Donation
from db.session import SessionLocal, init_db
from services.processing import METHODS, expected_components, separation_windows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconstruct separation records for pre-existing units."
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    init_db()
    session = SessionLocal()

    try:
        component_code = {
            row.id: row.code for row in session.scalars(select(Component)).all()
        }

        # Donations that have units but no separation record behind them.
        donations = session.execute(
            select(
                Donation.id,
                Donation.din,
                Donation.facility_id,
                Donation.collected_at,
                Donation.bag_type,
                Donation.status,
            )
            .outerjoin(
                ComponentProduction, ComponentProduction.donation_id == Donation.id
            )
            .where(
                ComponentProduction.id.is_(None),
                # A TTI-reactive bag is discarded intact. Its existing units
                # are evidence of what was collected, not evidence that the bag
                # was separated, so reconstruction must never invent a
                # production event behind it.
                Donation.status != "QUARANTINED",
            )
        ).all()

        print(f"Donations with no separation record: {len(donations):,}")

        if not donations:
            print("Nothing to backfill.")
            return 0

        ids = [row.id for row in donations]
        units_by_donation: dict[str, list] = defaultdict(list)

        # Chunked, because SQLite caps the number of bound parameters and this
        # list runs to tens of thousands.
        for start in range(0, len(ids), 900):
            chunk = ids[start : start + 900]

            for unit in session.execute(
                select(
                    BloodUnit.donation_id,
                    BloodUnit.component_id,
                    BloodUnit.din,
                    BloodUnit.status,
                    BloodUnit.issued_at,
                    BloodUnit.discarded_at,
                ).where(BloodUnit.donation_id.in_(chunk))
            ).all():
                units_by_donation[unit.donation_id].append(unit)

        windows = separation_windows()
        rows = []
        skipped = 0
        window_missed = 0
        with_loss = 0

        for donation in donations:
            units = units_by_donation.get(donation.id, [])

            if not units:
                # A donation with no units at all is one still awaiting the lab,
                # or a quarantined one whose bag was discarded. Neither was
                # separated, and inventing a record would be a lie.
                skipped += 1
                continue

            produced = sorted(
                {
                    component_code.get(unit.component_id)
                    for unit in units
                    if component_code.get(unit.component_id)
                }
            )
            expected = expected_components(donation.bag_type)

            # A unit that is only whole blood was never separated.
            if produced == ["WB"] and "WB" in expected:
                skipped += 1
                continue

            collected = as_utc(donation.collected_at)

            # Separation happened after collection and before anything left the
            # shelf. Within that, put it early — a bank spins promptly, and the
            # platelet window means it usually had to.
            earliest_use = min(
                [
                    as_utc(unit.issued_at) or DEMO_DATETIME
                    for unit in units
                    if unit.issued_at
                ]
                or [DEMO_DATETIME]
            )
            latest = min(collected + timedelta(hours=6), earliest_use)
            produced_at = max(collected + timedelta(hours=2), collected)

            if produced_at > latest:
                produced_at = latest

            elapsed_hours = (produced_at - collected).total_seconds() / 3600.0

            # EXPECTED IS WHAT WAS PRODUCED, and no loss is recorded.
            #
            # This is the honest reading. The expected recipe was never captured
            # for these separations — the field did not exist — so inferring it
            # from the bag type and calling the difference a loss would invent a
            # 79% shortfall rate and put it in the yield report as though it had
            # been measured. "We do not know what was expected" is the truth, and
            # a report can exclude these rather than be misled by them.
            expected = list(produced)
            losses = {}

            rows.append(
                {
                    "id": str(uuid.uuid4()),
                    "donation_id": donation.id,
                    "facility_id": donation.facility_id,
                    "produced_at": produced_at,
                    "method": METHODS.get(
                        str(donation.bag_type or "").upper(), "CENTRIFUGATION"
                    ),
                    "recipe_code": donation.bag_type,
                    "units_expected": len(expected),
                    "units_produced": len(produced),
                    "expected_components": expected,
                    "produced_components": produced,
                    "loss_reasons": losses or None,
                    "minutes_from_collection": int(elapsed_hours * 60),
                    "produced_by": None,
                    "notes": (
                        "Reconstructed from the units this donation produced. The "
                        "separation predates the processing module: the operator, "
                        "the expected recipe and any shortfall were never recorded, "
                        "so expected is set equal to produced rather than inferred. "
                        "Yield reporting should exclude these."
                    ),
                }
            )

        print(f"  reconstructing: {len(rows):,}")
        print(f"  skipped (never separated): {skipped:,}")
        print(
            "  expected set equal to produced: the recipe was never recorded "
            "for these"
        )

        if args.dry_run:
            print()
            print("Dry run; nothing written.")
            return 0

        for start in range(0, len(rows), 5000):
            session.bulk_insert_mappings(
                ComponentProduction, rows[start : start + 5000]
            )

        session.commit()

        total = session.scalar(select(func.count()).select_from(ComponentProduction))
        orphaned = session.scalar(
            select(func.count())
            .select_from(BloodUnit)
            .outerjoin(
                ComponentProduction,
                ComponentProduction.donation_id == BloodUnit.donation_id,
            )
            .where(
                BloodUnit.donation_id.is_not(None),
                ComponentProduction.id.is_(None),
            )
        )

        print()
        print(f"Separation records: {total:,}")
        print(f"Units with no separation behind them: {orphaned:,}")

        return 0

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
