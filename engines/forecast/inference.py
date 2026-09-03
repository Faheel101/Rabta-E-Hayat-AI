"""Multi-step forecast generation.

Backtest and production inference share this module deliberately. If the
backtest evaluated one-step-ahead predictions using true lagged values while
production rolled predictions forward recursively, the measured WAPE would
describe a model nobody runs, and acceptance criterion 3 would be meaningless.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from config.calendar import CALENDAR_FEATURES, get_calendar_flags
from engines.forecast.features import (
    FEATURES,
    LAGS,
    ROLLING_WINDOWS,
    SERIES_FEATURES,
)
from engines.forecast.models import (
    QUANTILES,
    calendar_multiplier,
    count_quantiles,
    dispersion,
    fit_day_of_week_profile,
    seasonal_naive,
    sort_quantiles,
    trailing_mean,
    tsb_forecast,
)

HISTORY_BUFFER = max(max(LAGS), max(ROLLING_WINDOWS))

LGBM_REGIMES = {"SMOOTH", "ERRATIC"}
TSB_REGIMES = {"INTERMITTENT", "LUMPY"}


def build_series_states(panel_df, keys=None):
    """Per-series history tail and static features, ready for rolling forward."""

    states = {}

    columns = ["y", "demand_date"] + SERIES_FEATURES

    for key, group in panel_df.groupby(
        ["facility_id", "component_id", "blood_group_id"], sort=False
    ):
        if keys is not None and key not in keys:
            continue

        ordered = group.sort_values("demand_date")
        last = ordered.iloc[-1]

        states[key] = {
            "history": ordered["y"].to_numpy(dtype=float),
            "dates": ordered["demand_date"].tolist(),
            "statics": {name: float(last[name]) for name in SERIES_FEATURES},
        }

    return states


def _feature_row(state_history, statics, target_date: date, component_id: int):
    history = state_history
    padded = history

    if len(padded) < HISTORY_BUFFER:
        padded = np.concatenate(
            [np.zeros(HISTORY_BUFFER - len(padded)), padded]
        )

    row = dict(statics)
    row["component_id"] = float(component_id)

    flags = get_calendar_flags(target_date)
    for name in CALENDAR_FEATURES:
        row[name] = float(flags.get(name, 0))

    row["dayofweek"] = float(target_date.weekday())
    row["day"] = float(target_date.day)
    row["month"] = float(target_date.month)
    row["weekofyear"] = float(target_date.isocalendar()[1])
    row["is_weekend"] = float(target_date.weekday() >= 5)

    for lag in LAGS:
        row[f"lag_{lag}"] = float(padded[-lag])

    for window in ROLLING_WINDOWS:
        row[f"roll_mean_{window}"] = float(np.mean(padded[-window:]))

    for window in (7, 28):
        window_values = padded[-window:]
        row[f"roll_std_{window}"] = (
            float(np.std(window_values, ddof=1)) if len(window_values) > 1 else 0.0
        )

    row["roll_max_28"] = float(np.max(padded[-28:]))
    row["zero_frac_28"] = float(np.mean(padded[-28:] == 0))

    return row


def forecast_lgbm(models, states, regimes, future_dates):
    """Recursive multi-step prediction for the gradient-boosted series.

    The P50 prediction is fed back as the next step's lag, which is the standard
    recursive scheme and the one production uses.
    """

    keys = [key for key, regime in regimes.items() if regime in LGBM_REGIMES]
    keys = [key for key in keys if key in states]

    if not keys:
        return {}

    working = {key: states[key]["history"].copy() for key in keys}
    results = {key: [] for key in keys}

    for target_date in future_dates:
        rows = [
            _feature_row(
                working[key],
                states[key]["statics"],
                target_date,
                key[1],
            )
            for key in keys
        ]

        frame = pd.DataFrame(rows, columns=FEATURES).astype(float)

        predictions = {
            alpha: models[alpha].predict(frame) for alpha in QUANTILES
        }

        for index, key in enumerate(keys):
            p10, p50, p90 = sort_quantiles(
                float(predictions[0.1][index]),
                float(predictions[0.5][index]),
                float(predictions[0.9][index]),
            )

            results[key].append((p10, p50, p90))
            working[key] = np.append(working[key], p50)

    return results


def forecast_statistical(states, regimes, future_dates, calendar_factors):
    """TSB for intermittent and lumpy series; scheduled series get a weekday and
    calendar profile on their own trailing level."""

    results = {}

    for key, state in states.items():
        regime = regimes.get(key, "NO_DEMAND")

        if regime == "NO_DEMAND":
            results[key] = ([(0.0, 0.0, 0.0)] * len(future_dates), "baseline-zero-v1")
            continue

        if regime in TSB_REGIMES:
            results[key] = (
                tsb_forecast(
                    state["history"],
                    state["dates"],
                    future_dates,
                    key[1],
                    calendar_factors,
                ),
                "tsb-seasonal-v2",
            )
            continue

        if regime == "SCHEDULED":
            history = state["history"]
            level = float(np.mean(history[-28:])) if len(history) else 0.0
            profile = fit_day_of_week_profile(history, state["dates"])
            dispersion_ratio = dispersion(history)

            rows = []

            for target_date in future_dates:
                mean = level * profile[target_date.weekday()]
                mean *= calendar_multiplier(key[1], target_date, calendar_factors)
                rows.append(count_quantiles(mean, dispersion_ratio))

            results[key] = (rows, "calendar-scheduled-v1")

    return results


def forecast_baselines(states, future_dates):
    """Layer 0 comparison bar, always computed (spec §6.3).

    Baselines get quantiles too, wrapped around their point forecast with the
    series' own dispersion. Without that a baseline cannot be scored on pinball
    loss, and pinball loss is the metric that matters for an intermittent series:
    the MAE-optimal point forecast for a series that is zero most days is simply
    zero, so P50 accuracy would always favour the trivial baseline while saying
    nothing about the safety stock the forecast is actually used for.
    """

    from engines.forecast.models import seasonal_naive, trailing_mean

    results = {}

    for key, state in states.items():
        history = state["history"]
        dispersion_ratio = dispersion(history)

        naive = seasonal_naive(history, len(future_dates))
        trailing = trailing_mean(history, len(future_dates))

        results[key] = {
            "seasonal_naive": naive,
            "trailing_mean": trailing,
            "seasonal_naive_quantiles": [
                count_quantiles(float(value), dispersion_ratio) for value in naive
            ],
        }

    return results


def estimate_noise_floor(values: np.ndarray, dates=None) -> float | None:
    """Approximate lower bound on achievable WAPE for this series.

    Reporting this next to WAPE is the difference between "the model is 34%
    wrong" and "the series is 30% unpredictable and the model captured nearly
    everything else". Acceptance criterion 3 fixes a 25% WAPE target without
    reference to how noisy the data is, so the target alone cannot distinguish a
    good model on hard data from a poor model on easy data.

    The estimate removes the structure any competent model would learn — the
    weekday profile and the local level — and treats the residual spread as
    irreducible. Taking sd(diff(y))/sqrt(2) instead, as a naive version of this
    did, counts real weekday variation as noise and inflates the floor above the
    model's own error, which is self-evidently wrong.

    It remains an approximation, not a proof: a model that also learns the
    calendar effects left in the residual would beat it.
    """

    if len(values) < 60:
        return None

    mean = float(np.mean(values))

    if mean <= 0:
        return None

    series = pd.Series(values, dtype=float)

    # Local level: centred window, so it tracks drift without absorbing the
    # weekday shape.
    level = (
        series.rolling(29, center=True, min_periods=8).mean().to_numpy(dtype=float)
    )

    if dates is not None:
        profile = fit_day_of_week_profile(values, list(dates))
        weekday_factor = np.array(
            [profile[day.weekday()] for day in dates], dtype=float
        )
    else:
        weekday_factor = np.ones(len(values), dtype=float)

    expected = level * weekday_factor
    valid = ~np.isnan(expected)

    if valid.sum() < 30:
        return None

    residual = values[valid] - expected[valid]

    # Robust spread, not the standard deviation. The residual still contains the
    # calendar spikes this estimate does not model — Eid ul-Adha trauma at 2.2x,
    # Muharram at 1.8x, an injected mass-casualty day at 6x. Those are learnable
    # (the model has features for every one of them), so letting a handful of
    # extreme days drive the variance inflates the "floor" above the model's own
    # error, which is a contradiction rather than a finding. The median absolute
    # deviation ignores them.
    median = float(np.median(residual))
    mad = float(np.median(np.abs(residual - median)))
    sigma = 1.4826 * mad

    if sigma <= 0:
        return None

    # For a unimodal distribution the minimum expected absolute deviation of any
    # point forecast is roughly 0.8 sigma.
    return float(0.8 * sigma / mean)
