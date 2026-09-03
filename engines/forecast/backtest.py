"""Rolling-origin backtest (spec §6.4).

Acceptance criteria 3 and 4 are numeric claims — WAPE <= 25% at a 7-day horizon
on dense series, beating seasonal naive on >= 80% of series, and
shortage-detection recall >= 0.75 at a 3-day lead. Before this module those
claims had nothing behind them: there was no backtest, no baseline comparison,
and no MODEL_FALLBACK flag anywhere in the codebase.

Every number reported here is measured, including the ones that are unflattering.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import numpy as np

from engines.forecast.features import KEY_COLS, classify_regime
from engines.forecast.inference import (
    build_series_states,
    estimate_noise_floor,
    forecast_baselines,
    forecast_lgbm,
    forecast_statistical,
)
from engines.forecast.models import (
    fit_calendar_factors,
    train_quantile_models,
)

HORIZON_BUCKETS = (7, 14, 30)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def wape(actual: np.ndarray, predicted: np.ndarray) -> float | None:
    total = float(np.sum(np.abs(actual)))

    if total <= 0:
        return None

    return float(np.sum(np.abs(actual - predicted)) / total)


def mae(actual: np.ndarray, predicted: np.ndarray) -> float:
    return float(np.mean(np.abs(actual - predicted))) if len(actual) else 0.0


def mase(actual: np.ndarray, predicted: np.ndarray, naive: np.ndarray) -> float | None:
    denominator = float(np.mean(np.abs(actual - naive)))

    if denominator <= 1e-9:
        return None

    return float(np.mean(np.abs(actual - predicted)) / denominator)


def pinball(actual: np.ndarray, predicted: np.ndarray, alpha: float) -> float:
    error = actual - predicted

    return float(np.mean(np.maximum(alpha * error, (alpha - 1.0) * error)))


def picp(actual: np.ndarray, lower: np.ndarray, upper: np.ndarray) -> float:
    if len(actual) == 0:
        return 0.0

    return float(np.mean((actual >= lower) & (actual <= upper)))


# ---------------------------------------------------------------------------
# Folds
# ---------------------------------------------------------------------------


def build_folds(history_end: date, folds: int, test_window_days: int):
    """Rolling origin, expanding train, most recent fold last."""

    windows = []

    for index in range(folds):
        test_end = history_end - timedelta(days=index * test_window_days)
        test_start = test_end - timedelta(days=test_window_days - 1)
        windows.append((test_start, test_end))

    return list(reversed(windows))


def _collect(
    records,
    key,
    horizon_bucket,
    actual,
    p10,
    p50,
    p90,
    naive,
    trailing,
    naive_p90,
):
    bucket = records[(key, horizon_bucket)]
    bucket["actual"].append(actual)
    bucket["p10"].append(p10)
    bucket["p50"].append(p50)
    bucket["p90"].append(p90)
    bucket["naive"].append(naive)
    bucket["trailing"].append(trailing)
    bucket["naive_p90"].append(naive_p90)


def run_backtest(
    panel_df,
    history_end: date,
    folds: int = 8,
    test_window_days: int = 30,
    availability=None,
    seed: int = 42,
    log=print,
):
    """Returns (per-series metrics, network summary)."""

    from engines.forecast.features import FEATURES

    windows = build_folds(history_end, folds, test_window_days)

    actual_lookup = {}
    for row in panel_df.itertuples():
        actual_lookup[
            (row.facility_id, row.component_id, row.blood_group_id, row.demand_date)
        ] = float(row.y)

    records = defaultdict(
        lambda: {
            "actual": [],
            "p10": [],
            "p50": [],
            "p90": [],
            "naive": [],
            "trailing": [],
            "naive_p90": [],
        }
    )
    regime_by_key = {}

    # Irreducible noise, measured once per series over the full history rather
    # than per fold, so it is stable and independent of any model.
    noise_floor = {}

    for key, group in panel_df.groupby(KEY_COLS, sort=False):
        ordered = group.sort_values("demand_date")
        noise_floor[key] = estimate_noise_floor(
            ordered["y"].to_numpy(dtype=float),
            ordered["demand_date"].tolist(),
        )

    shortage_events = 0
    shortage_flagged = 0

    # Rolled up to facility x component. Forecasts are produced per blood group
    # because the optimizer needs that grain, but a storekeeper orders by
    # component and then allocates across groups. Error at the ordering grain is
    # the decision-relevant number, and averaging over eight groups cancels a
    # large part of the per-group noise.
    aggregate = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))

    for fold_index, (test_start, test_end) in enumerate(windows, start=1):
        train_mask = panel_df["demand_date"] < test_start
        train_df = panel_df.loc[train_mask]

        if train_df.empty:
            continue

        log(
            f"  fold {fold_index}/{len(windows)}  train<{test_start}  "
            f"test {test_start}..{test_end}  rows={len(train_df):,}"
        )

        fitted = train_df.dropna(subset=FEATURES + ["y"])
        models = train_quantile_models(fitted, FEATURES, seed=seed)
        calendar_factors = fit_calendar_factors(train_df)

        states = build_series_states(train_df)

        regimes = {}
        for key, state in states.items():
            regime = classify_regime(state["history"][-150:])
            regimes[key] = regime
            regime_by_key[key] = regime

        future_dates = [
            test_start + timedelta(days=offset) for offset in range(test_window_days)
        ]

        lgbm_rows = forecast_lgbm(models, states, regimes, future_dates) if models else {}
        statistical_rows = forecast_statistical(
            states, regimes, future_dates, calendar_factors
        )
        baselines = forecast_baselines(states, future_dates)

        for key in states:
            if key in lgbm_rows:
                quantile_rows = lgbm_rows[key]
            elif key in statistical_rows:
                quantile_rows = statistical_rows[key][0]
            else:
                continue

            naive = baselines[key]["seasonal_naive"]
            trailing = baselines[key]["trailing_mean"]
            naive_quantiles = baselines[key]["seasonal_naive_quantiles"]

            for offset, target_date in enumerate(future_dates):
                actual = actual_lookup.get(
                    (key[0], key[1], key[2], target_date)
                )

                if actual is None:
                    continue

                p10, p50, p90 = quantile_rows[offset]

                for bucket in HORIZON_BUCKETS:
                    if offset >= bucket:
                        continue

                    _collect(
                        records,
                        key,
                        bucket,
                        actual,
                        p10,
                        p50,
                        p90,
                        float(naive[offset]),
                        float(trailing[offset]),
                        float(naive_quantiles[offset][2]),
                    )

                    cell = aggregate[(key[0], key[1], bucket)][(fold_index, offset)]
                    cell[0] += actual
                    cell[1] += p50

            if availability:
                events, flagged = _score_shortage_detection(
                    key,
                    future_dates,
                    quantile_rows,
                    actual_lookup,
                    availability,
                )
                shortage_events += events
                shortage_flagged += flagged

    metrics = _summarise_series(records, regime_by_key, noise_floor)
    summary = _summarise_network(
        metrics, regime_by_key, shortage_events, shortage_flagged
    )
    summary["wape_component_grain_7d"] = _aggregate_wape(aggregate, 7)

    return metrics, summary


def _aggregate_wape(aggregate, horizon_bucket: int):
    """Volume-weighted WAPE at facility x component grain."""

    actual_total = 0.0
    error_total = 0.0

    for (_, _, bucket), cells in aggregate.items():
        if bucket != horizon_bucket:
            continue

        for actual, predicted in cells.values():
            actual_total += abs(actual)
            error_total += abs(actual - predicted)

    if actual_total <= 0:
        return None

    return error_total / actual_total


def _score_shortage_detection(
    key,
    future_dates,
    quantile_rows,
    actual_lookup,
    availability,
    lead_days: int = 3,
):
    """Spec §6.4: of days where actual demand exceeded on-hand stock, what
    fraction did P90 flag at least three days ahead?

    Operationalised as: standing at day d - lead, with the stock visible then,
    does the cumulative P90 over the lead window exceed available stock; and did
    cumulative actual demand over that window in fact exceed it.
    """

    events = 0
    flagged = 0

    for offset in range(lead_days, len(future_dates)):
        origin_date = future_dates[offset - lead_days]
        available = availability.get((key[0], key[1], key[2], origin_date))

        if available is None:
            continue

        window = range(offset - lead_days + 1, offset + 1)

        actual_total = 0.0
        p90_total = 0.0
        complete = True

        for index in window:
            target_date = future_dates[index]
            actual = actual_lookup.get((key[0], key[1], key[2], target_date))

            if actual is None:
                complete = False
                break

            actual_total += actual
            p90_total += quantile_rows[index][2]

        if not complete or actual_total <= 0:
            continue

        if actual_total > available:
            events += 1

            if p90_total > available:
                flagged += 1

    return events, flagged


SPARSE_REGIMES = {"INTERMITTENT", "LUMPY"}


def _summarise_series(records, regime_by_key, noise_floor):
    results = []

    for (key, horizon_bucket), bucket in records.items():
        actual = np.array(bucket["actual"], dtype=float)
        p10 = np.array(bucket["p10"], dtype=float)
        p50 = np.array(bucket["p50"], dtype=float)
        p90 = np.array(bucket["p90"], dtype=float)
        naive = np.array(bucket["naive"], dtype=float)
        trailing = np.array(bucket["trailing"], dtype=float)
        naive_p90 = np.array(bucket["naive_p90"], dtype=float)

        regime = regime_by_key.get(key, "UNKNOWN")

        model_wape = wape(actual, p50)
        naive_wape = wape(actual, naive)
        trailing_wape = wape(actual, trailing)

        model_pinball_p90 = pinball(actual, p90, 0.90)
        naive_pinball_p90 = pinball(actual, naive_p90, 0.90)

        # Which metric decides "better" depends on what the series is for. A
        # dense series drives a point order quantity, so WAPE decides. An
        # intermittent series drives a safety stock, so the upper quantile
        # decides — and spec §6.4 says as much: pinball loss at P10/P90 is
        # "what the safety stock depends on". Judging an intermittent series on
        # P50 accuracy would retire every model in favour of predicting zero.
        if regime in SPARSE_REGIMES:
            beats = model_pinball_p90 <= naive_pinball_p90
        else:
            beats = (
                model_wape is not None
                and naive_wape is not None
                and trailing_wape is not None
                and model_wape <= naive_wape
                and model_wape <= trailing_wape
            )

        results.append(
            {
                "facility_id": key[0],
                "component_id": key[1],
                "blood_group_id": key[2],
                "horizon_days": horizon_bucket,
                "regime": regime,
                "n_observations": int(len(actual)),
                "actual_total": float(actual.sum()),
                "wape": model_wape,
                "wape_noise_floor": noise_floor.get(key),
                "mae": mae(actual, p50),
                "mase": mase(actual, p50, naive),
                "pinball_p10": pinball(actual, p10, 0.10),
                "pinball_p90": model_pinball_p90,
                "baseline_pinball_p90": naive_pinball_p90,
                "picp": picp(actual, p10, p90),
                "baseline_seasonal_naive_wape": naive_wape,
                "baseline_trailing_mean_wape": trailing_wape,
                "beats_baselines": bool(beats),
                # Spec §6.3: a series whose model loses to its own baseline falls
                # back to the baseline and is labelled, not quietly shipped.
                "is_fallback": bool(not beats and actual.sum() > 0),
            }
        )

    return results


DENSE_REGIMES = {"SMOOTH", "ERRATIC"}


def _summarise_network(metrics, regime_by_key, shortage_events, shortage_flagged):
    at_7d = [m for m in metrics if m["horizon_days"] == 7 and m["wape"] is not None]

    dense = [m for m in at_7d if m["regime"] in DENSE_REGIMES]

    def weighted_wape(rows):
        total_actual = sum(row["actual_total"] for row in rows)

        if total_actual <= 0:
            return None

        return sum(
            row["wape"] * row["actual_total"] for row in rows
        ) / total_actual

    def weighted_floor(rows):
        eligible = [r for r in rows if r["wape_noise_floor"] is not None]
        total_actual = sum(row["actual_total"] for row in eligible)

        if total_actual <= 0:
            return None

        return sum(
            row["wape_noise_floor"] * row["actual_total"] for row in eligible
        ) / total_actual

    return {
        "series_total": len({(m["facility_id"], m["component_id"], m["blood_group_id"]) for m in at_7d}),
        "series_dense": len(dense),
        "series_fallback": sum(1 for m in at_7d if m["is_fallback"]),
        "wape_dense_7d": weighted_wape(dense),
        "wape_all_7d": weighted_wape(at_7d),
        "noise_floor_dense_7d": weighted_floor(dense),
        "noise_floor_all_7d": weighted_floor(at_7d),
        "pct_series_beating_naive": (
            100.0 * sum(1 for m in at_7d if m["beats_baselines"]) / len(at_7d)
            if at_7d
            else None
        ),
        # Reported separately by regime. A P10-P90 band cannot cover exactly 80%
        # of a distribution that is a point mass at zero most days, so a blended
        # coverage number tells you nothing about either group.
        "picp_p10_p90": (
            float(np.mean([m["picp"] for m in dense])) if dense else None
        ),
        "picp_sparse": (
            float(
                np.mean(
                    [m["picp"] for m in at_7d if m["regime"] in SPARSE_REGIMES]
                )
            )
            if any(m["regime"] in SPARSE_REGIMES for m in at_7d)
            else None
        ),
        "shortage_detection_recall_3d": (
            shortage_flagged / shortage_events if shortage_events else None
        ),
        "shortage_events": shortage_events,
        "shortage_flagged": shortage_flagged,
        "regime_distribution": {
            regime: sum(1 for value in regime_by_key.values() if value == regime)
            for regime in sorted(set(regime_by_key.values()))
        },
    }
