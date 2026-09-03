"""Forecast backtest entry point (spec §6.4).

    python -m scripts.run_backtest              # 8 folds, as specified
    python -m scripts.run_backtest --folds 3    # quick check during development

Prints the acceptance-criteria numbers with an explicit pass or fail. A backtest
that only reports flattering metrics is not a backtest.
"""

from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta, timezone

import pandas as pd
from sqlalchemy import insert, select

from config.settings import DEMO_DATE
from core import config
from db.models import (
    Component,
    Facility,
    ForecastMetric,
    ForecastRunSummary,
    InventorySnapshot,
    new_id,
)
from db.session import SessionLocal, init_db
from engines.forecast.backtest import run_backtest
from engines.forecast.features import add_lag_features, build_panel, load_series

DEMO_DATETIME = datetime.combine(DEMO_DATE, time(8, 0, 0), tzinfo=timezone.utc)
HISTORY_END = DEMO_DATE - timedelta(days=1)
HISTORY_START = DEMO_DATE - timedelta(days=config.HISTORY_DAYS)

TARGET_WAPE_DENSE_7D = 0.25
TARGET_PCT_BEATING_NAIVE = 80.0
TARGET_SHORTAGE_RECALL = 0.75
TARGET_PICP = (0.70, 0.90)


def load_availability(session):
    """On-hand stock per series per day, for shortage-detection scoring."""

    frame = pd.read_sql(
        select(
            InventorySnapshot.facility_id,
            InventorySnapshot.component_id,
            InventorySnapshot.blood_group_id,
            InventorySnapshot.snapshot_date,
            InventorySnapshot.units_available,
        ),
        session.bind,
    )

    if frame.empty:
        return {}

    frame["snapshot_date"] = pd.to_datetime(frame["snapshot_date"]).dt.date

    return {
        (row.facility_id, row.component_id, row.blood_group_id, row.snapshot_date): float(
            row.units_available
        )
        for row in frame.itertuples()
    }


def report(summary, metrics):
    print()
    print("=" * 68)
    print("BACKTEST RESULTS (spec §6.4)")
    print("=" * 68)

    print(f"  series evaluated              {summary['series_total']:>8}")
    print(f"  dense series (SMOOTH/ERRATIC) {summary['series_dense']:>8}")
    print(f"  series on MODEL_FALLBACK      {summary['series_fallback']:>8}")
    print()
    print("  regime distribution:")
    for regime, count in sorted(summary["regime_distribution"].items()):
        print(f"    {regime:14s} {count:5d}")

    def line(label, value, target_text, passed, fmt="{:.1%}"):
        if value is None:
            print(f"  {label:<34} {'n/a':>8}   {target_text}")
            return

        mark = "PASS" if passed else "FAIL"
        print(f"  {label:<34} {fmt.format(value):>8}   {target_text}  [{mark}]")

    print()
    print("  Acceptance criteria:")

    wape_dense = summary["wape_dense_7d"]
    line(
        "WAPE, dense series, 7d horizon",
        wape_dense,
        "target <= 25%",
        wape_dense is not None and wape_dense <= TARGET_WAPE_DENSE_7D,
    )

    floor_dense = summary.get("noise_floor_dense_7d")

    if floor_dense is not None and wape_dense is not None:
        headroom = wape_dense - floor_dense
        print(
            f"    irreducible noise floor          {floor_dense:>8.1%}"
            f"   model is {headroom:+.1%} above the floor"
        )

    component_wape = summary.get("wape_component_grain_7d")

    if component_wape is not None:
        mark = "PASS" if component_wape <= TARGET_WAPE_DENSE_7D else "FAIL"
        print(
            f"  WAPE at facility x component grain {component_wape:>8.1%}"
            f"   target <= 25%  [{mark}]"
        )

    beating = summary["pct_series_beating_naive"]
    line(
        "series beating seasonal naive",
        beating / 100.0 if beating is not None else None,
        "target >= 80%",
        beating is not None and beating >= TARGET_PCT_BEATING_NAIVE,
    )

    recall = summary["shortage_detection_recall_3d"]
    line(
        "shortage-detection recall, 3d lead",
        recall,
        "target >= 75%",
        recall is not None and recall >= TARGET_SHORTAGE_RECALL,
    )

    coverage = summary["picp_p10_p90"]
    line(
        "P10-P90 coverage, dense series",
        coverage,
        "target ~80%",
        coverage is not None and TARGET_PICP[0] <= coverage <= TARGET_PICP[1],
    )

    sparse_coverage = summary.get("picp_sparse")

    if sparse_coverage is not None:
        print(
            f"    same, intermittent series        {sparse_coverage:>8.1%}"
            "   necessarily high: mostly zeros"
        )

    print()
    print(f"  WAPE, all series, 7d horizon   {summary['wape_all_7d']:.1%}"
          if summary["wape_all_7d"] is not None else "  WAPE, all series: n/a")
    print(
        f"  shortage events observed       {summary['shortage_events']:>8}"
        f"  flagged {summary['shortage_flagged']}"
    )


def main(folds: int = 8, test_window_days: int = 30):
    """Callable directly by scripts.rebuild, which must not have its own
    command-line arguments parsed by this module."""

    args = argparse.Namespace(folds=folds, test_window_days=test_window_days)

    init_db()
    session = SessionLocal()

    try:
        facility_df, component_df, group_df, agg_df = load_series(
            session, HISTORY_START, HISTORY_END
        )

        print("Building daily panel...")
        panel = build_panel(
            facility_df, component_df, group_df, agg_df, HISTORY_START, HISTORY_END
        )
        panel = add_lag_features(panel)
        print(f"  panel rows: {len(panel):,}")

        print("Loading on-hand history for shortage-detection scoring...")
        availability = load_availability(session)
        print(f"  snapshot points: {len(availability):,}")

        print(f"Running rolling-origin backtest ({args.folds} folds)...")
        metrics, summary = run_backtest(
            panel,
            HISTORY_END,
            folds=args.folds,
            test_window_days=args.test_window_days,
            availability=availability,
            seed=config.SEED,
        )

        run_id = new_id()

        metric_rows = [
            {
                "id": new_id(),
                "run_id": run_id,
                "model_version": "lgbm-quantile-v2"
                if row["regime"] in {"SMOOTH", "ERRATIC"}
                else "tsb-seasonal-v2",
                "folds": args.folds,
                "generated_at": DEMO_DATETIME,
                **row,
            }
            for row in metrics
        ]

        session.query(ForecastMetric).delete(synchronize_session=False)
        session.query(ForecastRunSummary).delete(synchronize_session=False)
        session.flush()

        for start in range(0, len(metric_rows), 5000):
            session.execute(insert(ForecastMetric), metric_rows[start : start + 5000])
            session.flush()

        session.execute(
            insert(ForecastRunSummary),
            [
                {
                    "id": new_id(),
                    "run_id": run_id,
                    "generated_at": DEMO_DATETIME,
                    "series_total": summary["series_total"],
                    "series_dense": summary["series_dense"],
                    "series_fallback": summary["series_fallback"],
                    "wape_dense_7d": summary["wape_dense_7d"],
                    "wape_all_7d": summary["wape_all_7d"],
                    "noise_floor_dense_7d": summary["noise_floor_dense_7d"],
                    "pct_series_beating_naive": summary["pct_series_beating_naive"],
                    "picp_p10_p90": summary["picp_p10_p90"],
                    "shortage_detection_recall_3d": summary[
                        "shortage_detection_recall_3d"
                    ],
                    "metrics_json": summary,
                }
            ],
        )

        session.commit()

        report(summary, metrics)
        print()
        print(f"Stored {len(metric_rows):,} per-series metric rows (run {run_id}).")

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the forecast backtest.")
    parser.add_argument(
        "--folds",
        type=int,
        default=8,
        help="Rolling-origin folds (spec §6.4 specifies 8).",
    )
    parser.add_argument("--test-window-days", type=int, default=30)
    parsed = parser.parse_args()

    main(folds=parsed.folds, test_window_days=parsed.test_window_days)
