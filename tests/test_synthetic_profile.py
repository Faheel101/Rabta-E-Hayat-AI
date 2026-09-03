"""Facility-scale synthetic profile invariants."""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import numpy as np

from core import config
from datagen.demand import VOLUME_SCALE
from datagen.run import DONORS_PER_FACILITY, generate_donors


def test_profile_keeps_long_forecast_history_but_bounds_operational_materialisation():
    assert config.HISTORY_DAYS >= 365
    assert 0 < VOLUME_SCALE <= 1
    assert 7 <= int(config.get("synthetic.unit_history_days")) <= 21
    assert 5 <= int(config.get("synthetic.operational_window_days")) <= 21


def test_register_generation_hits_each_facility_target_exactly():
    facilities = [
        SimpleNamespace(
            id="tertiary",
            organization_id="org-a",
            facility_type="TERTIARY_HOSPITAL",
            district="Lahore",
        ),
        SimpleNamespace(
            id="dhq",
            organization_id="org-b",
            facility_type="DHQ",
            district="Kasur",
        ),
    ]
    groups = [
        SimpleNamespace(id=1, population_pct_pk=70.0),
        SimpleNamespace(id=2, population_pct_pk=30.0),
    ]

    rows = generate_donors(facilities, groups, np.random.default_rng(42))
    counts = Counter(row["registered_facility_id"] for row in rows)

    assert counts["tertiary"] == DONORS_PER_FACILITY["TERTIARY_HOSPITAL"]
    assert counts["dhq"] == DONORS_PER_FACILITY["DHQ"]
    assert all(row["organization_id"] for row in rows)
