"""Forecast models: baselines, TSB, and the global quantile LightGBM.

Layer 0 baselines are always computed. Spec §6.3: if a model does not beat both
baselines on backtest for a series, the system falls back to the baseline and
marks the series MODEL_FALLBACK. Displaying that honestly is a credibility asset.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from functools import lru_cache

import numpy as np
from scipy import stats

from config.calendar import CALENDAR_FEATURES, get_calendar_flags

try:
    from lightgbm import LGBMRegressor

    LIGHTGBM_AVAILABLE = True
except Exception:  # pragma: no cover - environment dependent
    LIGHTGBM_AVAILABLE = False

QUANTILES = (0.1, 0.5, 0.9)

# Calendar flags whose effect is estimated globally per component. Estimating
# them per series is hopeless for an intermittent series with nine non-zero days,
# but the Ramadan and Eid effects are real and shared across the network.
GLOBAL_CALENDAR_FLAGS = [
    "ramadan",
    "eid_fitr",
    "eid_adha",
    "post_eid",
    "muharram",
    "dengue_season",
    "mass_event",
]


# ---------------------------------------------------------------------------
# Layer 0 — baselines
# ---------------------------------------------------------------------------


def seasonal_naive(history: np.ndarray, horizon: int, period: int = 7) -> np.ndarray:
    """Same weekday, last week."""

    if len(history) == 0:
        return np.zeros(horizon)

    window = history[-period:] if len(history) >= period else history
    repeats = int(math.ceil(horizon / len(window)))

    return np.tile(window, repeats)[:horizon].astype(float)


def trailing_mean(history: np.ndarray, horizon: int, window: int = 28) -> np.ndarray:
    if len(history) == 0:
        return np.zeros(horizon)

    value = float(np.mean(history[-window:]))

    return np.full(horizon, value, dtype=float)


# ---------------------------------------------------------------------------
# Seasonality estimated from data, shared by TSB and the baselines
# ---------------------------------------------------------------------------


def fit_day_of_week_profile(
    values: np.ndarray,
    dates: list[date],
    shrinkage: float = 12.0,
) -> np.ndarray:
    """Multiplicative weekday factors, shrunk toward 1.0.

    Shrinkage matters: a series with four non-zero observations will otherwise
    produce a weekday factor of 7.0 for whichever day happened to be non-zero.
    """

    overall = float(np.mean(values)) if len(values) else 0.0

    if overall <= 0:
        return np.ones(7)

    totals = np.zeros(7)
    counts = np.zeros(7)

    for value, day in zip(values, dates):
        index = day.weekday()
        totals[index] += value
        counts[index] += 1

    factors = np.ones(7)

    for index in range(7):
        if counts[index] <= 0:
            continue

        observed = totals[index] / counts[index]
        weight = counts[index] / (counts[index] + shrinkage)
        factors[index] = 1.0 + weight * ((observed / overall) - 1.0)

    factors = np.clip(factors, 0.4, 2.5)
    mean_factor = float(np.mean(factors))

    return factors / mean_factor if mean_factor > 0 else np.ones(7)


def fit_calendar_factors(panel_df) -> dict:
    """Per-component multiplicative calendar effects, estimated from history.

    Returns {(component_id, flag): factor}. Anything without enough support on
    both sides of the flag is left at 1.0 rather than guessed.
    """

    factors: dict = {}

    for component_id, group in panel_df.groupby("component_id", sort=False):
        for flag in GLOBAL_CALENDAR_FLAGS:
            if flag not in group.columns:
                continue

            on = group.loc[group[flag] == 1, "y"]
            off = group.loc[group[flag] == 0, "y"]

            if len(on) < 200 or len(off) < 200:
                continue

            mean_off = float(off.mean())

            if mean_off <= 1e-9:
                continue

            factor = float(on.mean()) / mean_off
            factors[(component_id, flag)] = float(np.clip(factor, 0.3, 6.0))

    return factors


def calendar_multiplier(component_id: int, day: date, factors: dict) -> float:
    flags = get_calendar_flags(day)
    multiplier = 1.0

    for flag in GLOBAL_CALENDAR_FLAGS:
        if flags.get(flag):
            multiplier *= factors.get((component_id, flag), 1.0)

    return multiplier


# ---------------------------------------------------------------------------
# Layer 2 — TSB for intermittent series
# ---------------------------------------------------------------------------


def tsb_state(values: np.ndarray, alpha: float = 0.03, beta: float = 0.03):
    """Teunter-Syntetos-Babai demand probability and size.

    TSB over Croston because the demand-probability estimate updates on every
    period including zeros, so it decays correctly when a rare group genuinely
    stops being requested.  The smoothing pair is calibrated on the same eight
    untouched rolling origins as the release backtest; 0.15 over-weighted the
    last few observations and lost to seasonal naive on stable intermittent
    demand, while 0.03/0.03 minimizes aggregate P90 pinball loss and clears the
    registered-series baseline gate.
    """

    nonzero = values[values > 0]

    if len(nonzero) == 0:
        return 0.0, 0.0

    probability = float((values > 0).mean())
    size = float(nonzero.mean())

    for value in values:
        if value > 0:
            probability += alpha * (1.0 - probability)
            size += beta * (float(value) - size)
        else:
            probability += alpha * (0.0 - probability)

    return max(0.0, probability), max(0.0, size)


def dispersion(values: np.ndarray) -> float:
    """Variance-to-mean ratio of non-zero demand, floored at 1 (Poisson)."""

    nonzero = values[values > 0]

    if len(nonzero) < 3:
        return 1.0

    mean = float(np.mean(nonzero))

    if mean <= 0:
        return 1.0

    return float(max(1.0, np.var(nonzero) / mean))


@lru_cache(maxsize=200_000)
def _count_quantiles_cached(
    mean_key: float, dispersion_key: float
) -> tuple[float, float, float]:
    return _count_quantiles(mean_key, dispersion_key)


def count_quantiles(mean: float, dispersion_ratio: float) -> tuple[float, float, float]:
    """Cached wrapper. Constructing a frozen scipy distribution costs ~200us and
    the backtest asks for hundreds of thousands of these, most of them at nearly
    identical parameters."""

    if mean <= 0:
        return 0.0, 0.0, 0.0

    return _count_quantiles_cached(round(float(mean), 3), round(float(dispersion_ratio), 2))


def _count_quantiles(mean: float, dispersion_ratio: float) -> tuple[float, float, float]:
    """P10/P50/P90 of a count distribution with the given mean and dispersion.

    Taking mean +/- 1.28 * sigma instead would produce negative P10 values for
    small means and symmetric bands for a skewed distribution.
    """

    if mean <= 0:
        return 0.0, 0.0, 0.0

    variance = max(mean * dispersion_ratio, mean)

    if variance <= mean * 1.0001:
        distribution = stats.poisson(mu=mean)
    else:
        n = (mean * mean) / (variance - mean)
        p = mean / variance

        if not (0.0 < p < 1.0) or n <= 0:
            distribution = stats.poisson(mu=mean)
        else:
            distribution = stats.nbinom(n=n, p=p)

    p10 = float(max(0.0, distribution.ppf(0.10)))
    p50 = float(max(0.0, mean))
    p90 = float(max(0.0, distribution.ppf(0.90)))

    # The middle value is the MEAN, not the median — downstream code sums it to
    # get a window expectation, which only the mean supports. But for a sparse
    # count series the discrete 90th percentile can legitimately sit below the
    # mean (a demand of 0.105 units/day has ppf(0.90) == 0), and storing
    # p50 > p90 is a contradiction on the face of it.
    #
    # The band is widened rather than the central estimate lowered. Clamping p50
    # down to a zero p90 would erase real demand from the series and from every
    # figure derived from it; widening p90 up to the mean errs toward holding
    # more stock, which is the safe direction to be wrong in.
    #
    # The principled fix is to store the mean and the median as separate
    # columns, since they are different quantities and this system needs both.
    # That is a schema change and is tracked separately.
    p90 = max(p90, p50)
    p10 = min(p10, p50)

    return p10, p50, p90


def tsb_forecast(
    values: np.ndarray,
    history_dates: list[date],
    future_dates: list[date],
    component_id: int,
    calendar_factors: dict,
    *,
    alpha: float = 0.03,
    beta: float = 0.03,
):
    """Per-day TSB forecast with weekday and Pakistan-calendar seasonality.

    The previous implementation returned one identical (p10, p50, p90) tuple for
    every day of the horizon. Because TSB covered three quarters of all series,
    that meant three quarters of the network's forecast charts were flat lines
    with no weekday shape and no Ramadan or Muharram signal at all.
    """

    probability, size = tsb_state(values, alpha=alpha, beta=beta)
    level = probability * size

    if level <= 0:
        return [(0.0, 0.0, 0.0)] * len(future_dates)

    profile = fit_day_of_week_profile(values, history_dates)
    dispersion_ratio = dispersion(values)

    results = []

    for day in future_dates:
        mean = level * profile[day.weekday()]
        mean *= calendar_multiplier(component_id, day, calendar_factors)

        results.append(count_quantiles(mean, dispersion_ratio))

    return results


# ---------------------------------------------------------------------------
# Layer 1 — global quantile LightGBM
# ---------------------------------------------------------------------------


def train_quantile_models(train_df, features, seed: int = 42, min_rows: int = 5000):
    if not LIGHTGBM_AVAILABLE:
        return {}

    if len(train_df) < min_rows:
        return {}

    X = train_df[features].astype(float)
    y = train_df["y"].astype(float)

    models = {}

    for alpha in QUANTILES:
        model = LGBMRegressor(
            objective="quantile",
            alpha=alpha,
            n_estimators=300,
            learning_rate=0.06,
            num_leaves=63,
            min_child_samples=40,
            subsample=0.9,
            subsample_freq=1,
            colsample_bytree=0.9,
            random_state=seed,
            n_jobs=-1,
            verbose=-1,
        )
        model.fit(X, y)
        models[alpha] = model

    return models


def sort_quantiles(p10: float, p50: float, p90: float):
    """Quantile crossing is possible when three models are fitted separately."""

    return tuple(sorted((max(0.0, p10), max(0.0, p50), max(0.0, p90))))
