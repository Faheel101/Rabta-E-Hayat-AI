"""Rebuild the demo database from scratch, in dependency order.

This is the spec §3.3 nightly cycle run end to end, and the only supported way
to get a coherent database: every stage reads the output of the previous one, so
running them out of order produces a plan built on stale forecasts.

    python -m scripts.rebuild            # full rebuild, drops all tables
    python -m scripts.rebuild --from forecast   # re-run from a stage onward

Seeded and reproducible: the same seed produces the same numbers every run, so
the demo cannot shift under a re-run (spec §15.5).
"""

from __future__ import annotations

import argparse
import time

from core.clock import DEMO_DATETIME
from db.base import Base
from db.models import IntelligenceRefreshState
from db.session import SessionLocal, engine, init_db
from services.intelligence_refresh import STATE_ID

# Order matters and the dependencies are not obvious, which is why the whole
# chain lives here rather than in a runbook. Four stages used to be missing and
# were run by hand — tenants, DIN reissue, donor identities and the operational
# history — so "rebuild" produced a database the application could not sign into
# and whose units had no provenance.
#
# The engine stages run twice on purpose. `operations` discards units on
# reactive TTI results, which changes what is on the shelf, so any risk score,
# transfer plan or mart computed before it is stale the moment it finishes.
STAGES = [
    ("reference", "scripts.seed_reference", "Reference data, facilities, compatibility"),
    ("tenants", "scripts.seed_tenants", "Organizations, facilities and user accounts"),
    ("synthetic", "scripts.generate_synthetic", "Synthetic history and inventory"),
    ("dins", "scripts.migrate_dins", "Reissue unit identifiers in ISBT 128 layout"),
    ("donors", "datagen.donors", "Donor identities for the seed register"),
    ("operations", "datagen.operations", "Donations, screening, TTI panel, processing"),
    # Catches any unit the generator produced without a separation behind
    # it. Normally a no-op; it exists so a partial run cannot leave the
    # traceability chain with a hole in it.
    ("separations", "scripts.backfill_processing", "Reconstruct missing separation records"),
    # After separations, because it places the components they produce, and it
    # derives each unit's cold-chain breach count from the readings it writes.
    ("storage", "datagen.storage", "Storage locations, temperature history, unit placement"),
    ("backtest", "scripts.run_backtest", "Forecast backtest and baselines"),
    ("forecast", "scripts.run_forecast", "Demand forecasts"),
    ("risk", "scripts.run_risk_rescue", "Shortage risk and expiry rescue"),
    ("optimizer", "scripts.run_optimizer", "Network transfer plan"),
    ("marts", "scripts.build_marts", "Analytical marts for the UI"),
]


def drop_all() -> None:
    print("Dropping all tables...")
    Base.metadata.drop_all(bind=engine)


def run_stage(module_name: str) -> float:
    """Run one stage's main() with a clean argv.

    Several stages parse their own command line (`migrate_dins --dry-run`,
    `operations --window-days`). Called in-process they would see rebuild's
    arguments instead of their own and fail on an unrecognised flag, so each one
    is given an empty argv and therefore its documented defaults.
    """

    import importlib
    import sys

    module = importlib.import_module(module_name)

    saved_argv = sys.argv
    sys.argv = [module_name]

    started = time.perf_counter()

    try:
        module.main()
    finally:
        sys.argv = saved_argv

    return time.perf_counter() - started


def finalize_intelligence_state(stage_names: set[str]) -> None:
    """Publish a clean freshness marker after the complete decision chain.

    The application worker creates a DIRTY baseline when no marker exists. A
    freshly rebuilt database already contains current risk, optimizer and mart
    outputs, so leaving the marker absent incorrectly tells every user that the
    just-built intelligence is stale.
    """

    if not {"risk", "optimizer", "marts"}.issubset(stage_names):
        return

    session = SessionLocal()
    try:
        state = session.get(IntelligenceRefreshState, STATE_ID)
        if state is None:
            state = IntelligenceRefreshState(
                id=STATE_ID,
                source_version=1,
                completed_version=1,
            )
            session.add(state)
        else:
            state.completed_version = max(
                int(state.completed_version or 0), int(state.source_version or 0)
            )

        state.status = "CLEAN"
        state.reason = "PIPELINE_REBUILD"
        state.requested_by = "scripts.rebuild"
        state.completed_at = DEMO_DATETIME
        state.failed_at = None
        state.last_error = None
        state.result_json = {"stages": sorted(stage_names)}
        state.updated_at = DEMO_DATETIME
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild the Rabta-e-Hayat database.")
    parser.add_argument(
        "--from",
        dest="from_stage",
        choices=[name for name, _, _ in STAGES],
        help="Start from this stage instead of a full rebuild.",
    )
    parser.add_argument(
        "--only",
        dest="only_stage",
        choices=[name for name, _, _ in STAGES],
        help="Run a single stage.",
    )
    args = parser.parse_args()

    if args.only_stage:
        selected = [s for s in STAGES if s[0] == args.only_stage]
    elif args.from_stage:
        index = [name for name, _, _ in STAGES].index(args.from_stage)
        selected = STAGES[index:]
    else:
        drop_all()
        selected = STAGES

    init_db()

    timings = []

    for name, module_name, description in selected:
        print()
        print("=" * 72)
        print(f"STAGE {name} — {description}")
        print("=" * 72)

        elapsed = run_stage(module_name)
        timings.append((name, elapsed))

    finalize_intelligence_state({name for name, _, _ in selected})

    print()
    print("=" * 72)
    print("Rebuild complete.")
    for name, elapsed in timings:
        print(f"  {name:12s} {elapsed:7.1f}s")
    print(f"  {'total':12s} {sum(e for _, e in timings):7.1f}s")


if __name__ == "__main__":
    main()
