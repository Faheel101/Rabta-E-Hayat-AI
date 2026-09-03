"""Clinical demand request generation (spec §15.2).

Produces `units_requested` per (facility x component x group x day). What is
actually *issued* is decided later by the inventory simulation in supply.py, not
here — a facility can only issue what it holds. Drawing a fill rate at random,
independently of stock, is what made the old generator's fill rate a constant of
nature rather than a consequence of supply.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from config.calendar import get_calendar_flags
from core import config

DISPERSION_K = float(config.get("supply.demand_dispersion_k", 20.0))
VOLUME_SCALE = float(config.get("synthetic.demand_volume_scale", 1.0))

# Units requested per bed per day, before seasonality and specialty effects.
COMPONENT_RATES = {
    "PRBC": {
        "RBC": 0.030,
        "TERTIARY_HOSPITAL": 0.050,
        "SPECIALIST_CENTRE": 0.040,
        "DHQ": 0.020,
        "THQ": 0.010,
    },
    "WB": {
        "RBC": 0.004,
        "TERTIARY_HOSPITAL": 0.006,
        "SPECIALIST_CENTRE": 0.003,
        "DHQ": 0.004,
        "THQ": 0.002,
    },
    "PLT_RD": {
        "RBC": 0.010,
        "TERTIARY_HOSPITAL": 0.020,
        "SPECIALIST_CENTRE": 0.025,
        "DHQ": 0.005,
        "THQ": 0.002,
    },
    "PLT_APH": {
        "RBC": 0.003,
        "TERTIARY_HOSPITAL": 0.006,
        "SPECIALIST_CENTRE": 0.010,
        "DHQ": 0.001,
        "THQ": 0.0005,
    },
    "FFP": {
        "RBC": 0.008,
        "TERTIARY_HOSPITAL": 0.015,
        "SPECIALIST_CENTRE": 0.010,
        "DHQ": 0.006,
        "THQ": 0.003,
    },
    "CRYO": {
        "RBC": 0.003,
        "TERTIARY_HOSPITAL": 0.005,
        "SPECIALIST_CENTRE": 0.003,
        "DHQ": 0.002,
        "THQ": 0.001,
    },
}

PLATELET_COMPONENTS = {"PLT_RD", "PLT_APH"}
RED_COMPONENTS = {"PRBC", "WB"}
ELECTIVE_COMPONENTS = {"PRBC", "FFP", "WB"}


def daterange(start_date: date, end_date: date):
    current = start_date

    while current <= end_date:
        yield current
        current += timedelta(days=1)


def effective_beds(facility) -> int:
    if facility.facility_type == "RBC":
        return 300

    return max(int(facility.bed_count or 0), 40)


def specialty_multiplier(facility, component_code: str) -> float:
    multiplier = 1.0

    if component_code in PLATELET_COMPONENTS and facility.has_oncology:
        multiplier *= 2.2

    if component_code == "PRBC" and facility.has_thalassaemia_centre:
        multiplier *= 1.8

    if component_code in {"PRBC", "FFP"} and facility.has_obgyn:
        multiplier *= 1.15

    if component_code in {"PRBC", "WB", "FFP"} and facility.has_trauma_centre:
        multiplier *= 1.2

    if component_code in {"PRBC", "FFP", "PLT_APH"} and facility.has_cardiac_surgery:
        multiplier *= 1.15

    return multiplier


def calendar_multiplier(day: date, facility, component_code: str) -> float:
    """Weekly, annual and Hijri seasonality (spec §15.2)."""

    flags = get_calendar_flags(day)
    weekday = day.weekday()

    multiplier = 1.0

    # Elective surgery rhythm: Mon/Tue peak, Fri/Sun trough.
    if component_code in ELECTIVE_COMPONENTS:
        if weekday in (0, 1):
            multiplier *= 1.12
        elif weekday == 2:
            multiplier *= 1.05
        elif weekday == 4:
            multiplier *= 0.95
        elif weekday == 5:
            multiplier *= 0.92
        elif weekday == 6:
            multiplier *= 0.88

    if facility.has_trauma_centre and component_code in {
        "PRBC",
        "WB",
        "FFP",
        "PLT_RD",
    }:
        if weekday in (4, 5):
            multiplier *= 1.18

    # Ramadan: elective work is deferred. The collection-side collapse is
    # modelled in supply.py, which is the whole point — demand and supply move
    # in opposite directions.
    if flags["ramadan"]:
        if component_code in ELECTIVE_COMPONENTS:
            multiplier *= 0.86
        if component_code in PLATELET_COMPONENTS:
            multiplier *= 0.94

    if flags["post_eid"] and component_code in ELECTIVE_COMPONENTS:
        multiplier *= 1.25

    # Eid ul-Adha butchering injuries and Muharram processions.
    if flags["eid_adha"] and facility.has_trauma_centre and component_code in {
        "PRBC",
        "WB",
    }:
        multiplier *= 2.2

    if flags["muharram"] and facility.has_trauma_centre and component_code in {
        "PRBC",
        "WB",
        "FFP",
    }:
        multiplier *= 1.8

    if component_code in PLATELET_COMPONENTS and flags["dengue_season"]:
        multiplier *= 1.65

    if component_code in {"FFP", "PRBC"} and flags["heat_season"] and day.month in (6, 7):
        multiplier *= 1.08

    if flags["mass_event"] and facility.has_trauma_centre and component_code in {
        "PRBC",
        "WB",
        "FFP",
        "PLT_RD",
    }:
        multiplier *= 6.0

    return multiplier


def sample_count(mean: float, rng: np.random.Generator) -> int:
    """Over-dispersed count draw. Demand is count data, never Gaussian.

    The dispersion parameter k controls how noisy daily demand is:
    variance = mean + mean^2 / k, so CV^2 = 1/mean + 1/k.

    This was previously hardcoded at k=5, which gives a coefficient of variation
    of about 0.50 for a large hospital's single-group daily demand — meaning
    demand routinely halves or doubles from one day to the next. That is not what
    transfusion demand looks like, and it has a measurable consequence: it puts
    an irreducible floor of roughly 40% on any forecast's WAPE, so the spec's
    25% accuracy target becomes unreachable no matter how good the model is. The
    backtest now reports that noise floor alongside WAPE so the two can never be
    confused again.
    """

    if mean <= 0.004:
        return 0

    if mean < 2.5:
        return int(rng.poisson(mean))

    k = DISPERSION_K

    return int(rng.negative_binomial(k, k / (k + mean)))


def choose_clinical_context(facility, component_code: str, day: date, rng) -> str:
    flags = get_calendar_flags(day)
    r = float(rng.random())

    trauma = 0.10 if facility.has_trauma_centre else 0.03
    if flags["eid_adha"] or flags["muharram"] or flags["mass_event"]:
        trauma *= 2.2

    oncology = 0.04
    if facility.has_oncology:
        oncology = 0.10
    if facility.has_oncology and component_code in PLATELET_COMPONENTS:
        oncology = 0.18

    thalassaemia = 0.0
    if facility.has_thalassaemia_centre and component_code == "PRBC":
        thalassaemia = 0.22

    obstetric = 0.16 if facility.has_obgyn else 0.02

    surgery = 0.24
    if flags["ramadan"]:
        surgery *= 0.45
    if flags["post_eid"]:
        surgery *= 1.3

    for threshold, label in (
        (trauma, "TRAUMA"),
        (oncology, "ONCOLOGY"),
        (thalassaemia, "THALASSAEMIA"),
        (obstetric, "OBSTETRIC"),
        (surgery, "SURGERY_ELECTIVE"),
        (0.18, "MEDICAL"),
    ):
        if r < threshold:
            return label
        r -= threshold

    return "OTHER"


def choose_urgency(context: str, day: date, rng) -> str:
    flags = get_calendar_flags(day)
    r = float(rng.random())

    if context == "TRAUMA":
        if flags["mass_event"]:
            if r < 0.15:
                return "MASSIVE_TRANSFUSION"
            if r < 0.55:
                return "EMERGENCY"
            return "URGENT"

        if r < 0.05:
            return "MASSIVE_TRANSFUSION"
        if r < 0.30:
            return "EMERGENCY"
        if r < 0.75:
            return "URGENT"
        return "ROUTINE"

    if context in {"OBSTETRIC", "ONCOLOGY", "THALASSAEMIA"}:
        if r < 0.02:
            return "EMERGENCY"
        if r < 0.25:
            return "URGENT"
        return "ROUTINE"

    if r < 0.01:
        return "EMERGENCY"
    if r < 0.18:
        return "URGENT"

    return "ROUTINE"


def build_demand_requests(facilities, components, groups, days, group_probs, rng):
    """Requested units per series per day.

    Returns {(facility_id, component_id, group_id): np.ndarray of length len(days)}
    plus a parallel map of the sampled clinical context and urgency per request,
    so demand_event rows can be written later without re-drawing.
    """

    requests = {}
    attributes = {}

    for facility in facilities:
        beds = effective_beds(facility)

        for component in components:
            base_rate = COMPONENT_RATES.get(component.code, {}).get(
                facility.facility_type,
                0.005,
            )

            specialty = specialty_multiplier(facility, component.code)

            calendar_by_day = np.array(
                [calendar_multiplier(day, facility, component.code) for day in days],
                dtype=float,
            )

            for group_index, group in enumerate(groups):
                share = float(group_probs[group_index])

                means = (
                    beds
                    * base_rate
                    * share
                    * calendar_by_day
                    * specialty
                    * VOLUME_SCALE
                )

                series = np.zeros(len(days), dtype=np.int32)
                series_attributes = {}

                for day_index, mean in enumerate(means):
                    units = sample_count(float(mean), rng)

                    if units <= 0:
                        continue

                    units = min(units, 90)
                    series[day_index] = units

                    day = days[day_index]
                    context = choose_clinical_context(
                        facility, component.code, day, rng
                    )

                    series_attributes[day_index] = (
                        context,
                        choose_urgency(context, day, rng),
                    )

                key = (facility.id, component.id, group.id)

                if series.sum() > 0:
                    requests[key] = series
                    attributes[key] = series_attributes

    return requests, attributes
