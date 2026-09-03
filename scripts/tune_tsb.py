"""Read-only calibration harness for intermittent-demand smoothing.

The production backtest is deliberately expensive because it trains three
global LightGBM models at every rolling origin.  Tuning TSB's two smoothing
parameters should not retrain those unrelated models for every candidate.  This
script reuses the exact eight rolling origins, regime classifier, calendar
factors, count distribution, and P90 pinball metric, but evaluates only the
INTERMITTENT/LUMPY cohorts that TSB actually serves.

It never writes to the database.  A parameter pair belongs in production only
after this harness identifies it and the complete backtest confirms it.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import timedelta

import numpy as np

from config.settings import DEMO_DATE
from core import config
from db.session import SessionLocal
from engines.forecast.backtest import build_folds, pinball
from engines.forecast.features import KEY_COLS, build_panel, classify_regime, load_series
from engines.forecast.models import (
    calendar_multiplier,
    count_quantiles,
    dispersion,
    fit_calendar_factors,
    fit_day_of_week_profile,
    seasonal_naive,
    tsb_state,
)

SPARSE_REGIMES = {"INTERMITTENT", "LUMPY"}
HISTORY_END = DEMO_DATE - timedelta(days=1)
HISTORY_START = DEMO_DATE - timedelta(days=config.HISTORY_DAYS)


def _values(raw: str) -> tuple[float, ...]:
    values = tuple(float(item.strip()) for item in raw.split(",") if item.strip())

    if not values or any(not 0 < value <= 1 for value in values):
        raise argparse.ArgumentTypeError("smoothing values must be in (0, 1]")

    return values


def evaluate(
    alphas: tuple[float, ...],
    betas: tuple[float, ...],
    *,
    folds: int = 8,
    test_window_days: int = 30,
) -> list[dict]:
    session = SessionLocal()

    try:
        facility_df, component_df, group_df, aggregate_df = load_series(
            session, HISTORY_START, HISTORY_END
        )
    finally:
        session.close()

    panel = build_panel(
        facility_df,
        component_df,
        group_df,
        aggregate_df,
        HISTORY_START,
        HISTORY_END,
    )

    actual_lookup = {
        (row.facility_id, row.component_id, row.blood_group_id, row.demand_date): float(row.y)
        for row in panel.itertuples()
    }
    candidates = [(alpha, beta) for alpha in alphas for beta in betas]
    actual_by_key: dict[tuple, list[float]] = defaultdict(list)
    baseline_by_key: dict[tuple, list[float]] = defaultdict(list)
    predicted_by_candidate: dict[tuple, dict[tuple, list[float]]] = {
        candidate: defaultdict(list) for candidate in candidates
    }

    windows = build_folds(HISTORY_END, folds, test_window_days)

    for fold_index, (test_start, test_end) in enumerate(windows, start=1):
        train = panel.loc[panel["demand_date"] < test_start]
        calendar_factors = fit_calendar_factors(train)
        future_dates = [test_start + timedelta(days=offset) for offset in range(7)]

        print(
            f"fold {fold_index}/{len(windows)}  train<{test_start}  "
            f"score {future_dates[0]}..{future_dates[-1]}"
        )

        for key, group in train.groupby(KEY_COLS, sort=False):
            ordered = group.sort_values("demand_date")
            history = ordered["y"].to_numpy(dtype=float)
            regime = classify_regime(history[-150:])

            if regime not in SPARSE_REGIMES:
                continue

            dates = ordered["demand_date"].tolist()
            profile = fit_day_of_week_profile(history, dates)
            dispersion_ratio = dispersion(history)
            naive = seasonal_naive(history, len(future_dates))

            actuals = [
                actual_lookup[(key[0], key[1], key[2], day)] for day in future_dates
            ]
            actual_by_key[key].extend(actuals)
            baseline_by_key[key].extend(
                count_quantiles(float(value), dispersion_ratio)[2] for value in naive
            )

            for alpha, beta in candidates:
                probability, size = tsb_state(history, alpha=alpha, beta=beta)
                level = probability * size
                predictions = predicted_by_candidate[(alpha, beta)][key]

                for day in future_dates:
                    mean = level * profile[day.weekday()]
                    mean *= calendar_multiplier(key[1], day, calendar_factors)
                    predictions.append(count_quantiles(mean, dispersion_ratio)[2])

    results = []

    for alpha, beta in candidates:
        passed = 0
        evaluated = 0
        model_loss = 0.0
        baseline_loss = 0.0

        for key, actual_values in actual_by_key.items():
            actual = np.asarray(actual_values, dtype=float)

            if actual.sum() <= 0:
                continue

            predicted = np.asarray(
                predicted_by_candidate[(alpha, beta)][key], dtype=float
            )
            baseline = np.asarray(baseline_by_key[key], dtype=float)
            candidate_loss = pinball(actual, predicted, 0.90)
            naive_loss = pinball(actual, baseline, 0.90)

            evaluated += 1
            passed += int(candidate_loss <= naive_loss)
            model_loss += candidate_loss
            baseline_loss += naive_loss

        results.append(
            {
                "alpha": alpha,
                "beta": beta,
                "evaluated": evaluated,
                "passed": passed,
                "pass_pct": 100.0 * passed / evaluated if evaluated else 0.0,
                "pinball_ratio": model_loss / baseline_loss if baseline_loss else None,
            }
        )

    return sorted(
        results,
        key=lambda row: (-row["pass_pct"], row["pinball_ratio"] or float("inf")),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Tune TSB smoothing read-only.")
    parser.add_argument("--alphas", type=_values, default=_values("0.03,0.05,0.08,0.10,0.15,0.20"))
    parser.add_argument("--betas", type=_values, default=_values("0.03,0.05,0.08,0.10,0.15,0.20"))
    parser.add_argument("--folds", type=int, default=8)
    parser.add_argument("--test-window-days", type=int, default=30)
    args = parser.parse_args()

    results = evaluate(
        args.alphas,
        args.betas,
        folds=args.folds,
        test_window_days=args.test_window_days,
    )

    print("\nalpha  beta   series  non-worse  pass%  pinball/baseline")

    for row in results[:15]:
        ratio = "n/a" if row["pinball_ratio"] is None else f"{row['pinball_ratio']:.3f}"
        print(
            f"{row['alpha']:>5.2f}  {row['beta']:>4.2f}  {row['evaluated']:>7}  "
            f"{row['passed']:>9}  {row['pass_pct']:>5.1f}  {ratio:>16}"
        )


if __name__ == "__main__":
    main()
