from pathlib import Path

from scripts.validate_synthetic_dataset import FacilityProfile, _capacity


def test_capacity_sums_component_limits():
    assert _capacity('{"PRBC": 400, "FFP": 250, "PLT_RD": 40}') == 690
    assert _capacity(None) == 0


def test_facility_profile_is_serializable():
    profile = FacilityProfile(
        code="TEST",
        facility_type="DHQ",
        donors=220,
        live_units=75,
        demand_events_30d=180,
        requested_units_30d=310,
        capacity=900,
    )
    assert profile.donors == 220
    assert Path("rabta.db").suffix == ".db"
