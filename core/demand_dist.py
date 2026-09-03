"""Turn forecast quantiles back into a demand distribution.

Spec §6.5 defines shortage_risk as P(demand > available), "estimated from the
quantile spread", and §7.2 defines waste_probability as
P(local_consumption < queue_position + 1). Both need an actual distribution, not
a piecewise interpolation between P50 and P90 — an interpolation cannot express
"available is far below P10" differently from "available is just below P50",
and it silently pins the answer to whatever the hand-tuned breakpoints were.

Blood demand is count data, so the fitted distribution is Poisson or negative
binomial (moment-matched), never Gaussian. sigma is recovered from the P10-P90
spread on the assumption of local symmetry, which is the best available
inference from three stored quantiles.
"""

from __future__ import annotations

import math

from scipy import stats

from core import config


def quantile_z() -> float:
    return float(config.get("demand_distribution.quantile_z", 1.2816))


def min_sigma() -> float:
    return float(config.get("demand_distribution.min_sigma", 0.35))


def sigma_from_quantiles(p10: float, p90: float) -> float:
    """Recover a standard deviation from a P10-P90 band."""

    spread = max(0.0, float(p90) - float(p10))
    sigma = spread / (2.0 * quantile_z())

    return max(sigma, min_sigma())


def window_moments(quantile_rows) -> tuple[float, float]:
    """Aggregate per-day (p10, p50, p90) triples into a window mean and sigma.

    Days are treated as independent, so variances add. That understates
    autocorrelation in a surge but is the standard safety-stock assumption.
    """

    mean = 0.0
    variance = 0.0

    for p10, p50, p90 in quantile_rows:
        mean += float(p50)
        sigma = sigma_from_quantiles(p10, p90)
        variance += sigma * sigma

    return mean, math.sqrt(variance) if variance > 0 else 0.0


def _distribution(mean: float, sigma: float):
    """Moment-match a count distribution to (mean, sigma)."""

    mean = max(0.0, float(mean))
    variance = max(float(sigma) ** 2, mean)

    if mean <= 0.0:
        return None

    if variance <= mean * 1.0001:
        return stats.poisson(mu=mean)

    n = (mean * mean) / (variance - mean)
    p = mean / variance

    if not (0.0 < p < 1.0) or n <= 0:
        return stats.poisson(mu=mean)

    return stats.nbinom(n=n, p=p)


def prob_demand_exceeds(available: float, mean: float, sigma: float) -> float:
    """P(demand > available). The shortage probability of spec §6.5."""

    distribution = _distribution(mean, sigma)

    if distribution is None:
        return 0.0

    threshold = math.floor(max(0.0, float(available)))

    return float(min(1.0, max(0.0, distribution.sf(threshold))))


def prob_demand_below(threshold: float, mean: float, sigma: float) -> float:
    """P(demand < threshold). The waste probability of spec §7.2."""

    distribution = _distribution(mean, sigma)

    if distribution is None:
        return 1.0 if threshold > 0 else 0.0

    ceiling = math.ceil(float(threshold)) - 1

    if ceiling < 0:
        return 0.0

    return float(min(1.0, max(0.0, distribution.cdf(ceiling))))


def bucket(
    probability: float,
    thresholds,
    *,
    available: float | None = None,
    reserve_floor: float | None = None,
    strategic_minimum: float | None = None,
) -> str:
    """Map a probability onto SAFE / WATCH / WARNING / CRITICAL.

    Probability is not the only input. A shelf that is already below its reserve
    floor is in trouble regardless of what demand is forecast to do — and when
    forecast demand is low the probability of running out is low, so a facility
    holding zero units of O-negative was being painted SAFE. Green for an empty
    shelf is the worst possible failure mode for this screen, because it is the
    one a user acts on by doing nothing.

    Stock position therefore sets a floor on severity that probability can raise
    but never lower.
    """

    safe, watch, warning = (float(value) for value in thresholds)

    floor_bucket = None

    if available is not None:
        if strategic_minimum is not None and available <= float(strategic_minimum):
            # Below the strategic minimum for a rare group: there is no buffer
            # left at all.
            floor_bucket = "CRITICAL"
        elif reserve_floor is not None and available <= float(reserve_floor):
            floor_bucket = "WARNING"

    if floor_bucket is not None:
        by_probability = _bucket_from_probability(probability, safe, watch, warning)
        return _more_severe(floor_bucket, by_probability)

    return _bucket_from_probability(probability, safe, watch, warning)


SEVERITY_ORDER = ["SAFE", "WATCH", "WARNING", "CRITICAL"]


def _more_severe(first: str, second: str) -> str:
    return max(first, second, key=SEVERITY_ORDER.index)


def _bucket_from_probability(
    probability: float, safe: float, watch: float, warning: float
) -> str:
    if probability < safe:
        return "SAFE"

    if probability < watch:
        return "WATCH"

    if probability < warning:
        return "WARNING"

    return "CRITICAL"


def risk_thresholds(component_code: str):
    from core.policy import PLATELET_COMPONENTS

    buckets = config.get("risk.buckets") or {}

    if component_code in PLATELET_COMPONENTS and "platelet" in buckets:
        return buckets["platelet"]

    return buckets.get("default", [0.10, 0.30, 0.60])
