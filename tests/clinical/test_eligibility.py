"""Donor eligibility invariants.

The first test is the one that matters most. The source research proposed
encoding a conditional deferral as zero days with a false permanent flag, and
the verification pass flagged it as clinically unsafe: a naive
`eligible = today >= deferred_until` would score a currently pregnant donor as
eligible. These tests exist so that cannot regress.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core import config, eligibility
from core.eligibility import CONDITIONAL, PERMANENT, TIMED

TODAY = date(2026, 8, 6)


def test_a_currently_pregnant_donor_is_never_eligible_today():
    """The regression this whole model exists to prevent."""

    assessment = eligibility.assess(
        sex="FEMALE",
        today=TODAY,
        age_years=30,
        haemoglobin_g_dl=13.0,
        weight_kg=60,
        systolic_bp=118,
        diastolic_bp=76,
        pulse_bpm=72,
        temperature_c=36.8,
        answers={"currently_pregnant": True},
    )

    assert not assessment.is_accepted
    assert "PREGNANCY_CURRENT" in assessment.blocking_reason_codes

    deferral = next(
        d for d in assessment.deferrals if d.reason_code == "PREGNANCY_CURRENT"
    )

    assert deferral.kind == CONDITIONAL
    assert deferral.until is None, (
        "a conditional deferral must not carry a date, or it will lapse into "
        "eligibility on its own"
    )

    # And the donor-record field must not be given a date either.
    assert assessment.deferred_until is None


def test_conditional_deferral_never_produces_a_past_due_date():
    for reason_code, spec in (config.get("deferral_rules") or {}).items():
        if str(spec.get("kind")).upper() != CONDITIONAL:
            continue

        deferral = eligibility.build_deferral(reason_code, today=TODAY)

        assert deferral.until is None, f"{reason_code} carries a date"
        assert deferral.kind == CONDITIONAL


def test_permanent_deferral_reports_no_expiry():
    assessment = eligibility.assess(
        sex="MALE",
        today=TODAY,
        answers={"infection_history": {"answer": True, "choice": "HIV"}},
    )

    assert assessment.is_permanent
    assert assessment.deferred_until is None


def test_hiv_disclosure_is_not_recorded_as_a_hepatitis_deferral():
    """Recording an HIV disclosure under a hepatitis code would corrupt any
    lookback and could send the donor the wrong notification."""

    assessment = eligibility.assess(
        sex="MALE",
        today=TODAY,
        answers={"infection_history": {"answer": True, "choice": "HIV"}},
    )

    assert "HIV_POSITIVE" in assessment.blocking_reason_codes
    assert "HEPATITIS_B_HISTORY" not in assessment.blocking_reason_codes


@pytest.mark.parametrize(
    "choice,expected",
    [
        ("HIV", "HIV_POSITIVE"),
        ("HEPATITIS_B", "HEPATITIS_B_HISTORY"),
        ("HEPATITIS_C", "HEPATITIS_C_HISTORY"),
    ],
)
def test_infection_history_limbs_map_to_distinct_codes(choice, expected):
    assessment = eligibility.assess(
        sex="MALE",
        today=TODAY,
        answers={"infection_history": {"answer": True, "choice": choice}},
    )

    assert expected in assessment.blocking_reason_codes


# ------------------------------------------------------------- sex-specific --


def test_inter_donation_interval_is_sex_specific():
    """The Punjab SOP: three months for men, four for women. Sex-dependence is
    structural, not a note — as written in the source research it lived in free
    text and every female donor would have been assessed on the male interval."""

    assert eligibility.interval_days("MALE") == 90
    assert eligibility.interval_days("FEMALE") == 120

    hundred_days_ago = TODAY - timedelta(days=100)

    male = eligibility.assess(
        sex="MALE", today=TODAY, last_donation_on=hundred_days_ago
    )
    female = eligibility.assess(
        sex="FEMALE", today=TODAY, last_donation_on=hundred_days_ago
    )

    assert male.is_accepted, "100 days clears the 90-day male interval"
    assert not female.is_accepted, "100 days does not clear the 120-day female interval"
    assert "RECENT_DONATION" in female.blocking_reason_codes


def test_recent_donation_deferral_dates_from_the_donation_not_from_today():
    last = TODAY - timedelta(days=30)

    assessment = eligibility.assess(
        sex="MALE", today=TODAY, last_donation_on=last
    )

    deferral = next(
        d for d in assessment.deferrals if d.reason_code == "RECENT_DONATION"
    )

    assert deferral.until == last + timedelta(days=90)


# ------------------------------------------------------------------ vitals ---


def test_haemoglobin_uses_the_configured_method():
    threshold, method = eligibility.haemoglobin_threshold("MALE")

    assert method == "CUSO4"
    assert threshold == 12.5, (
        "the copper sulphate method is sex-neutral at 12.5; the 13.5 male value "
        "belongs to the haemoglobinometer method"
    )


def test_low_haemoglobin_defers_with_a_recheck_interval():
    assessment = eligibility.assess(
        sex="FEMALE", today=TODAY, haemoglobin_g_dl=11.0
    )

    deferral = next(
        d for d in assessment.deferrals if d.reason_code == "LOW_HAEMOGLOBIN"
    )

    assert deferral.kind == TIMED
    assert deferral.until == TODAY + timedelta(days=30)
    assert "haematinics" in deferral.detail


@pytest.mark.parametrize(
    "weight,expected_volume",
    [(70, 450), (52, 450), (50, 450), (47, 350), (45, 350), (44, None)],
)
def test_collection_volume_follows_the_two_step_ladder(weight, expected_volume):
    """A two-step ladder, not a proportional formula. A (weight/50)*450
    calculation silently changes the blood-to-anticoagulant ratio."""

    assert eligibility.collection_volume_for(weight) == expected_volume


def test_underweight_donor_is_deferred_rather_than_bled_short():
    assessment = eligibility.assess(sex="MALE", today=TODAY, weight_kg=42)

    assert "UNDERWEIGHT" in assessment.blocking_reason_codes
    assert assessment.collection_volume_ml is None


def test_reduced_volume_collection_warns_about_anticoagulant_ratio():
    assessment = eligibility.assess(
        sex="FEMALE",
        today=TODAY,
        age_years=28,
        weight_kg=47,
        haemoglobin_g_dl=13.0,
        systolic_bp=115,
        diastolic_bp=75,
        pulse_bpm=70,
        temperature_c=36.6,
    )

    assert assessment.is_accepted
    assert assessment.collection_volume_ml == 350
    assert any("anticoagulant" in warning for warning in assessment.warnings)


@pytest.mark.parametrize(
    "systolic,diastolic,deferred",
    [(120, 80, False), (95, 75, True), (150, 80, True), (120, 65, True), (120, 95, True)],
)
def test_blood_pressure_bounds(systolic, diastolic, deferred):
    assessment = eligibility.assess(
        sex="MALE", today=TODAY, systolic_bp=systolic, diastolic_bp=diastolic
    )

    assert ("BP_OUT_OF_RANGE" in assessment.blocking_reason_codes) is deferred


def test_fever_defers_as_acute_illness():
    assessment = eligibility.assess(sex="MALE", today=TODAY, temperature_c=38.2)

    assert "ACUTE_ILLNESS_FEVER" in assessment.blocking_reason_codes


@pytest.mark.parametrize("age,deferred", [(17, True), (18, False), (60, False), (61, True)])
def test_age_bounds(age, deferred):
    assessment = eligibility.assess(sex="MALE", today=TODAY, age_years=age)

    assert ("AGE_OUT_OF_RANGE" in assessment.blocking_reason_codes) is deferred


# ----------------------------------------------------------- questionnaire ---


def test_dated_answer_dates_the_deferral_from_the_event():
    """A tattoo eleven months ago defers for one more month, not twelve."""

    eleven_months_ago = TODAY - timedelta(days=335)

    assessment = eligibility.assess(
        sex="MALE",
        today=TODAY,
        answers={"tattoo_piercing": {"answer": True, "on": eleven_months_ago}},
    )

    deferral = next(
        d for d in assessment.deferrals if d.reason_code == "TATTOO_PIERCING"
    )

    assert deferral.until == eleven_months_ago + timedelta(days=365)
    assert (deferral.until - TODAY).days == 30


def test_pregnancy_question_is_not_asked_of_male_donors():
    keys = {question["key"] for question in eligibility.questions_for("MALE")}

    assert "currently_pregnant" not in keys
    assert "currently_pregnant" in {
        question["key"] for question in eligibility.questions_for("FEMALE")
    }


def test_a_clean_donor_is_accepted():
    assessment = eligibility.assess(
        sex="MALE",
        today=TODAY,
        age_years=34,
        haemoglobin_g_dl=14.2,
        weight_kg=72,
        systolic_bp=122,
        diastolic_bp=78,
        pulse_bpm=68,
        temperature_c=36.7,
        last_donation_on=TODAY - timedelta(days=200),
        answers={key: False for key in eligibility.QUESTION_BY_KEY},
    )

    assert assessment.is_accepted
    assert assessment.collection_volume_ml == 450
    assert assessment.deferred_until is None


# ------------------------------------------------------------- safety rails --


def test_existing_permanent_deferral_blocks_regardless_of_good_vitals():
    assessment = eligibility.assess(
        sex="MALE",
        today=TODAY,
        age_years=30,
        haemoglobin_g_dl=15.0,
        weight_kg=80,
        already_permanently_deferred=True,
    )

    assert assessment.is_permanent
    assert not assessment.is_accepted


def test_disabling_malaria_screening_forces_the_travel_deferral_back_on(monkeypatch):
    """Pakistan applies no domestic malaria travel deferral, which is only safe
    because every donation is screened. The two must be interlocked so an
    operator cannot create the unsafe combination by editing one key."""

    original = config.get

    def patched(path, default=None):
        if path == "tti_panel.malaria_screening_enabled":
            return False

        return original(path, default)

    monkeypatch.setattr(config, "get", patched)

    assessment = eligibility.assess(
        sex="MALE",
        today=TODAY,
        age_years=30,
        haemoglobin_g_dl=14.0,
        weight_kg=70,
    )

    assert not assessment.is_accepted
    assert any("malaria" in warning.lower() for warning in assessment.warnings)


def test_contested_rules_record_which_limb_was_applied():
    """A value nobody was told was contested will be treated as settled."""

    summary = eligibility.signoff_summary()
    codes = {entry["reason_code"] for entry in summary}

    for expected in ("SYPHILIS_HISTORY", "TUBERCULOSIS", "HEPATITIS_B_HISTORY"):
        assert expected in codes

    for entry in summary:
        assert entry["applied"], f"{entry['reason_code']} records no applied limb"
        assert entry["alternative"], f"{entry['reason_code']} records no alternative"
        assert entry["note"]


def test_applying_a_contested_rule_stamps_the_limb_on_the_assessment():
    assessment = eligibility.assess(
        sex="MALE",
        today=TODAY,
        answers={"infection_history": {"answer": True, "choice": "HEPATITIS_B"}},
    )

    assert assessment.signoff_notes
    assert any("HEPATITIS_B_HISTORY" in note for note in assessment.signoff_notes)


def test_every_questionnaire_item_maps_to_a_configured_rule():
    rules = config.get("deferral_rules") or {}

    for question in eligibility.QUESTIONS:
        codes = [question["reason_code"], *(question.get("choices") or {}).values()]

        for code in codes:
            assert code in rules, (
                f"question {question['key']} maps to {code}, which has no rule"
            )


def test_every_rule_declares_a_valid_kind_and_confidence():
    for code, spec in (config.get("deferral_rules") or {}).items():
        assert spec.get("kind") in {TIMED, CONDITIONAL, PERMANENT}, code
        assert spec.get("confidence") in {"high", "medium", "low"}, code

        if spec["kind"] == TIMED:
            assert (
                spec.get("days") is not None
                or spec.get("days_male") is not None
            ), f"{code} is TIMED but carries no duration"
