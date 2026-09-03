"""Invariants the vein-to-vein history must satisfy.

These are not statistical checks on plausibility — they are the clinical rules
that make the traceability chain mean something. A blood management system whose
records violate any of these is worse than one with no records, because it looks
authoritative while being wrong.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from core import config
from core.clock import DEMO_DATETIME
from db.models import (
    BloodUnit,
    Donation,
    DonationTest,
    DonorScreening,
)
from db.session import SessionLocal

ISSUED_STATUSES = ("TRANSFUSED", "CROSSMATCHED", "RESERVED", "ISSUED")


@pytest.fixture(scope="module")
def db():
    session = SessionLocal()

    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------- traceability


def test_every_unit_in_the_window_traces_to_a_donation(db):
    """A unit collected after go-live with no donation behind it is a unit you
    cannot recall, cannot look back on, and cannot investigate."""

    window_start = db.scalar(select(func.min(Donation.collected_at)))

    assert window_start is not None, "no donation history — run datagen.operations"

    orphans = db.scalar(
        select(func.count())
        .select_from(BloodUnit)
        .where(BloodUnit.collected_at >= window_start)
        .where(BloodUnit.donation_id.is_(None))
    )

    assert orphans == 0, f"{orphans:,} units in the window have no donation"


def test_every_donation_traces_to_a_donor_and_a_screening(db):
    missing_donor = db.scalar(
        select(func.count()).select_from(Donation).where(Donation.donor_id.is_(None))
    )
    missing_screening = db.scalar(
        select(func.count())
        .select_from(Donation)
        .where(Donation.screening_id.is_(None))
    )

    assert missing_donor == 0
    assert missing_screening == 0


def test_no_donation_references_a_donor_that_does_not_exist(db):
    dangling = db.scalar(
        text(
            "SELECT COUNT(*) FROM donation d "
            "LEFT JOIN donor dn ON dn.id = d.donor_id WHERE dn.id IS NULL"
        )
    )

    assert dangling == 0


# ------------------------------------------------------------ TTI safety rules


def test_no_reactive_unit_ever_reached_a_patient(db):
    """The one that matters most. If this ever fails, the system is asserting
    that somebody was transfused with infected blood."""

    placeholders = ", ".join(f":s{index}" for index in range(len(ISSUED_STATUSES)))
    params = {f"s{index}": value for index, value in enumerate(ISSUED_STATUSES)}

    leaked = db.scalar(
        text(
            "SELECT COUNT(*) FROM blood_unit u "
            "JOIN donation d ON d.id = u.donation_id "
            f"WHERE d.status = 'QUARANTINED' AND u.status IN ({placeholders})"
        ),
        params,
    )

    assert leaked == 0, f"{leaked:,} reactive units are recorded as issued"


def test_reactive_donations_are_never_released(db):
    released = db.scalar(
        select(func.count())
        .select_from(Donation)
        .where(Donation.status == "QUARANTINED")
        .where(Donation.released_at.is_not(None))
    )

    assert released == 0


def test_reactive_donations_produce_no_components(db):
    produced = db.scalar(
        text(
            "SELECT COUNT(*) FROM component_production p "
            "JOIN donation d ON d.id = p.donation_id "
            "WHERE d.status = 'QUARANTINED'"
        )
    )

    assert produced == 0


def test_every_reactive_donation_has_a_reactive_test_result(db):
    """Quarantine status and test results must agree. A quarantined donation
    with a clean panel means somebody's status field is lying."""

    inconsistent = db.scalar(
        text(
            "SELECT COUNT(*) FROM donation d "
            "WHERE d.status = 'QUARANTINED' AND NOT EXISTS ("
            "  SELECT 1 FROM donation_test t "
            "  WHERE t.donation_id = d.id AND t.is_reactive = 1)"
        )
    )

    assert inconsistent == 0


def test_no_released_donation_has_a_reactive_result(db):
    """The reverse direction."""

    # Driven from the reactive results (a few thousand) rather than from the
    # released donations (tens of thousands), which turns a correlated scan of
    # the whole test table into a handful of primary key lookups.
    leaked = db.scalar(
        text(
            "SELECT COUNT(*) FROM ("
            "  SELECT DISTINCT t.donation_id AS did FROM donation_test t "
            "  WHERE t.is_reactive = 1) r "
            "JOIN donation d ON d.id = r.did WHERE d.status = 'RELEASED'"
        )
    )

    assert leaked == 0


def test_every_completed_donation_ran_the_full_required_panel(db):
    """No released, quarantined or processed donation may have a short panel.

    COLLECTED and TESTING are real workflow states, not historical corruption:
    a newly collected bag has no results and a plate may have only part of its
    panel entered while the lab finishes the run.
    """

    required = list(config.get("tti_panel.required_tests"))

    if not config.get("tti_panel.malaria_screening_enabled"):
        required = [code for code in required if code != "MALARIA"]

    short = db.scalar(
        text(
            "SELECT COUNT(*) FROM ("
            "  SELECT d.id, COUNT(DISTINCT t.test_code) AS n "
            "  FROM donation d LEFT JOIN donation_test t ON t.donation_id = d.id "
            "  WHERE d.status NOT IN ('COLLECTED', 'TESTING') "
            "  GROUP BY d.id) WHERE n < :needed"
        ),
        {"needed": len(required)},
    )

    assert short == 0, f"{short:,} completed donations are missing part of the TTI panel"


# ------------------------------------------------------- segregation of duties


def test_nobody_verified_their_own_test_result(db):
    """Two-person release. Where a second technologist exists, verification must
    be a different person; where none exists the result stays unverified."""

    self_verified = db.scalar(
        select(func.count())
        .select_from(DonationTest)
        .where(DonationTest.verified_by.is_not(None))
        .where(DonationTest.verified_by == DonationTest.tested_by)
    )

    assert self_verified == 0


def test_verified_results_are_verified_after_they_were_tested(db):
    backwards = db.scalar(
        select(func.count())
        .select_from(DonationTest)
        .where(DonationTest.verified_at.is_not(None))
        .where(DonationTest.verified_at < DonationTest.tested_at)
    )

    assert backwards == 0


# ------------------------------------------------------------ donor behaviour


def test_no_donor_gave_twice_inside_the_minimum_interval(db):
    """90 days for men, 120 for women. Violating this is how you cause
    iron-deficiency anaemia in your own donor base."""

    violations = db.execute(
        text(
            "SELECT d.donor_id, COUNT(*) AS n FROM donation d "
            "JOIN donor dn ON dn.id = d.donor_id "
            "JOIN donation e ON e.donor_id = d.donor_id AND e.id <> d.id "
            "WHERE ABS(JULIANDAY(d.collected_at) - JULIANDAY(e.collected_at)) < "
            "  CASE WHEN dn.gender = 'FEMALE' THEN :female ELSE :male END "
            "GROUP BY d.donor_id LIMIT 5"
        ),
        {
            "male": int(config.get("donation_interval.whole_blood_days_male")),
            "female": int(config.get("donation_interval.whole_blood_days_female")),
        },
    ).all()

    assert not violations, f"donors gave inside the interval: {violations}"


def test_nobody_donated_after_being_permanently_deferred(db):
    """The invariant is chronological, not absolute.

    A donor confirmed positive for a transfusion-transmissible infection is
    permanently deferred *because* they donated — that donation is how the
    infection was found. What must never happen is a donation dated after the
    deferral was recorded.
    """

    after = db.scalar(
        text(
            "SELECT COUNT(*) FROM donation d "
            "JOIN donor_deferral f ON f.donor_id = d.donor_id "
            "WHERE f.is_permanent = 1 AND d.collected_at > f.deferred_at"
        )
    )

    assert after == 0, f"{after:,} donations were taken after a permanent deferral"


def test_permanently_deferred_seed_donors_never_donated(db):
    """Seed-register donors carry a permanent deferral from before go-live, so
    for them the absolute rule does hold."""

    donated = db.scalar(
        text(
            "SELECT COUNT(*) FROM donation d JOIN donor dn ON dn.id = d.donor_id "
            "WHERE dn.is_permanently_deferred = 1 "
            "  AND dn.donor_code NOT LIKE 'D-%' "
            "  AND NOT EXISTS (SELECT 1 FROM donor_deferral f "
            "                  WHERE f.donor_id = dn.id AND f.is_permanent = 1)"
        )
    )

    assert donated == 0


def test_donor_totals_match_the_donations_recorded(db):
    """A derived counter that disagrees with the rows it counts is a bug that
    surfaces as a wrong number on a dashboard.

    Two populations, two rules. Donors created in this system must match their
    donation rows exactly. Seed donors (PK-D-*) are migrated register entries
    whose totals include donations made before go-live, which by design have no
    rows here — for them the count may only be higher, never lower.
    """

    in_system_mismatch = db.scalar(
        text(
            "SELECT COUNT(*) FROM donor dn WHERE dn.donor_code LIKE 'D-%' "
            "AND dn.total_donations <> "
            "(SELECT COUNT(*) FROM donation d WHERE d.donor_id = dn.id)"
        )
    )

    assert in_system_mismatch == 0, (
        f"{in_system_mismatch:,} in-system donors claim a count they cannot evidence"
    )

    understated = db.scalar(
        text(
            "SELECT COUNT(*) FROM donor dn WHERE dn.donor_code NOT LIKE 'D-%' "
            "AND dn.total_donations < "
            "(SELECT COUNT(*) FROM donation d WHERE d.donor_id = dn.id)"
        )
    )

    assert understated == 0, (
        f"{understated:,} migrated donors have more donations than their total"
    )


def test_the_register_was_not_duplicated_by_a_re_run(db):
    """The generator is idempotent. If it were not, every run would leave the
    previous run's donors behind with counts that reference deleted rows."""

    orphaned = db.scalar(
        text(
            "SELECT COUNT(*) FROM donor dn WHERE dn.donor_code LIKE 'D-%' "
            "AND dn.total_donations > 0 "
            "AND NOT EXISTS (SELECT 1 FROM donation d WHERE d.donor_id = dn.id)"
        )
    )

    assert orphaned == 0, f"{orphaned:,} donors are left over from an earlier run"


def test_a_deferred_screening_never_produced_a_donation(db):
    leaked = db.scalar(
        text(
            "SELECT COUNT(*) FROM donation d "
            "JOIN donor_screening s ON s.id = d.screening_id "
            "WHERE s.outcome = 'DEFERRED'"
        )
    )

    assert leaked == 0


# ----------------------------------------------------------------- chronology


def test_screening_happens_before_collection(db):
    backwards = db.scalar(
        text(
            "SELECT COUNT(*) FROM donation d "
            "JOIN donor_screening s ON s.id = d.screening_id "
            "WHERE s.screened_at > d.collected_at"
        )
    )

    assert backwards == 0


def test_testing_happens_after_collection(db):
    backwards = db.scalar(
        text(
            "SELECT COUNT(*) FROM donation_test t "
            "JOIN donation d ON d.id = t.donation_id "
            "WHERE t.tested_at < d.collected_at"
        )
    )

    assert backwards == 0


def test_nothing_is_dated_after_the_demo_instant(db):
    for table, column in (
        ("donation", "collected_at"),
        ("donor_screening", "screened_at"),
        ("donation_test", "tested_at"),
        ("component_production", "produced_at"),
    ):
        future = db.scalar(
            # Compare temporal values, not SQLite's text encodings. Python 3.14
            # serializes the aware bound with an offset while stored UTC values
            # include microseconds; lexical comparison incorrectly calls an
            # event exactly at the demo instant "future".
            text(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE julianday({column}) > julianday(:now)"
            ),
            {"now": DEMO_DATETIME},
        )

        assert future == 0, f"{future:,} {table} rows are in the future"


# -------------------------------------------------------------------- realism


def test_the_reactive_rate_is_in_the_range_reported_for_pakistan(db):
    """Roughly 6-7% of donations trip at least one marker, HCV dominant. Well
    outside that range means the panel is miscalibrated in a way that would
    mislead anyone reading the lab dashboard."""

    total = db.scalar(select(func.count()).select_from(Donation))
    reactive = db.scalar(
        select(func.count())
        .select_from(Donation)
        .where(Donation.status == "QUARANTINED")
    )

    rate = reactive / total

    assert 0.04 <= rate <= 0.10, f"reactive rate {rate:.2%} is implausible"


def test_hcv_is_the_most_common_reactive_marker(db):
    rows = db.execute(
        select(DonationTest.test_code, func.count())
        .where(DonationTest.is_reactive.is_(True))
        .group_by(DonationTest.test_code)
        .order_by(func.count().desc())
    ).all()

    assert rows, "no reactive results at all"
    assert rows[0][0] == "HCV", f"expected HCV to dominate, got {rows}"


def test_deferral_rate_is_plausible(db):
    total = db.scalar(select(func.count()).select_from(DonorScreening))
    deferred = db.scalar(
        select(func.count())
        .select_from(DonorScreening)
        .where(DonorScreening.outcome == "DEFERRED")
    )

    rate = deferred / total

    assert 0.08 <= rate <= 0.20, f"deferral rate {rate:.2%} is implausible"


def test_low_haemoglobin_is_the_leading_deferral_reason(db):
    rows = db.execute(
        select(DonorScreening.deferral_reason_code, func.count())
        .where(DonorScreening.outcome == "DEFERRED")
        .group_by(DonorScreening.deferral_reason_code)
        .order_by(func.count().desc())
    ).all()

    assert rows[0][0] == "LOW_HAEMOGLOBIN", f"got {rows[:3]}"


def test_deferred_donors_have_haemoglobin_consistent_with_the_reason(db):
    """A donor deferred for low haemoglobin whose recorded haemoglobin is normal
    would make the screening record self-contradictory."""

    threshold = float(config.get("donor_eligibility.haemoglobin.cuso4_gdl_all"))

    contradictory = db.scalar(
        select(func.count())
        .select_from(DonorScreening)
        .where(DonorScreening.deferral_reason_code == "LOW_HAEMOGLOBIN")
        .where(DonorScreening.haemoglobin_g_dl >= threshold)
    )

    assert contradictory == 0


def test_accepted_donors_met_the_haemoglobin_threshold(db):
    threshold = float(config.get("donor_eligibility.haemoglobin.cuso4_gdl_all"))

    below = db.scalar(
        select(func.count())
        .select_from(DonorScreening)
        .where(DonorScreening.outcome == "ACCEPTED")
        .where(DonorScreening.haemoglobin_g_dl < threshold)
    )

    assert below == 0, f"{below:,} donors were bled below {threshold} g/dL"


# ------------------------------------------------------- confirmatory testing


def test_every_reactive_donor_is_deferred_in_some_form(db):
    """A donor whose donation screened reactive must not stay on the recall
    list. Whether that deferral is permanent depends on the confirmatory
    result — but there must be one."""

    free = db.scalar(
        text(
            "SELECT COUNT(DISTINCT d.donor_id) FROM donation d "
            "JOIN donor dn ON dn.id = d.donor_id "
            "WHERE d.status = 'QUARANTINED' "
            "  AND NOT EXISTS (SELECT 1 FROM donor_deferral f "
            "                  WHERE f.donor_id = dn.id)"
        )
    )

    assert free == 0, f"{free:,} reactive donors carry no deferral at all"


def test_permanent_deferral_requires_a_positive_confirmatory_result(db):
    """A reactive screen is not a diagnosis. Permanently deferring on the screen
    alone would wrongly label roughly half the donors it flags."""

    unjustified = db.scalar(
        text(
            "SELECT COUNT(*) FROM donor_deferral f "
            "WHERE f.is_permanent = 1 AND f.reason_code LIKE 'TTI_%' "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM donation d "
            "    JOIN donation_test t ON t.donation_id = d.id "
            "    WHERE d.donor_id = f.donor_id "
            "      AND t.test_group = 'TTI_CONFIRMATORY' "
            "      AND t.result = 'POSITIVE')"
        )
    )

    assert unjustified == 0, (
        f"{unjustified:,} donors permanently deferred without a positive "
        "confirmatory result"
    )


def test_an_unconfirmed_reactive_donor_is_not_labelled_infected(db):
    """The other direction. A negative confirmatory result must not leave the
    donor permanently deferred."""

    mislabelled = db.scalar(
        text(
            "SELECT COUNT(*) FROM donor_deferral f "
            "WHERE f.is_permanent = 1 "
            "  AND (f.reason_code LIKE 'TTI_UNCONFIRMED%' "
            "       OR f.reason_code LIKE 'TTI_AWAITING%')"
        )
    )

    assert mislabelled == 0


def test_a_confirmatory_test_is_a_different_assay_from_the_screen(db):
    """If the confirmatory row reuses the screening method, no second assay was
    actually run and the confirmation means nothing."""

    same_method = db.scalar(
        text(
            "SELECT COUNT(*) FROM donation_test c "
            "JOIN donation_test s ON s.donation_id = c.donation_id "
            "                    AND s.test_code = c.test_code "
            "                    AND s.test_group = 'TTI' "
            "WHERE c.test_group = 'TTI_CONFIRMATORY' AND c.method = s.method"
        )
    )

    assert same_method == 0


def test_confirmatory_testing_follows_the_screen(db):
    backwards = db.scalar(
        text(
            "SELECT COUNT(*) FROM donation_test c "
            "JOIN donation_test s ON s.donation_id = c.donation_id "
            "                    AND s.test_code = c.test_code "
            "                    AND s.test_group = 'TTI' "
            "WHERE c.test_group = 'TTI_CONFIRMATORY' AND c.tested_at < s.tested_at"
        )
    )

    assert backwards == 0


def test_only_reactive_screens_get_a_confirmatory_test(db):
    """Confirmatory assays are expensive and are run on reactive samples only.
    A confirmatory result against a clean screen means the two tiers have been
    wired together wrongly."""

    spurious = db.scalar(
        text(
            "SELECT COUNT(*) FROM donation_test c "
            "JOIN donation_test s ON s.donation_id = c.donation_id "
            "                    AND s.test_code = c.test_code "
            "                    AND s.test_group = 'TTI' "
            "WHERE c.test_group = 'TTI_CONFIRMATORY' AND s.is_reactive = 0"
        )
    )

    assert spurious == 0


# ------------------------------------------------------ donation composition


def test_a_donation_never_spans_more_than_one_blood_group(db):
    """One bag comes from one donor. Units of two different groups traced to a
    single donation is physically impossible, and it silently breaks a lookback:
    recalling that donation would pull units that came from someone else."""

    mixed = db.scalar(
        text(
            "SELECT COUNT(*) FROM (SELECT donation_id FROM blood_unit "
            "WHERE donation_id IS NOT NULL GROUP BY donation_id "
            "HAVING COUNT(DISTINCT blood_group_id) > 1)"
        )
    )

    assert mixed == 0, f"{mixed:,} donations span more than one blood group"


def test_every_unit_carries_the_donations_typed_group(db):
    contradicting = db.scalar(
        text(
            "SELECT COUNT(*) FROM blood_unit u "
            "JOIN donation d ON d.id = u.donation_id "
            "WHERE u.blood_group_id <> d.typed_blood_group_id"
        )
    )

    assert contradicting == 0


def test_a_donation_never_yields_two_units_of_the_same_component(db):
    """One bag, one red cell unit. Two would mean the recipe produced something
    the collection could not have contained."""

    duplicated = db.scalar(
        text(
            "SELECT COUNT(*) FROM (SELECT donation_id, component_id FROM blood_unit "
            "WHERE donation_id IS NOT NULL GROUP BY donation_id, component_id "
            "HAVING COUNT(*) > 1)"
        )
    )

    assert duplicated == 0


# ----------------------------------------------------------- age at donation


def _exact_age(born, on):
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))


def test_nobody_outside_the_accepted_age_range_was_bled(db):
    """Computed with exact date arithmetic.

    A JULIANDAY/365.25 approximation misreports thousands of donors sitting at
    the boundary, which is how this looked like a defect when it was not. Age is
    a calendar fact, not a division.
    """

    from datetime import date

    rows = db.execute(
        text(
            "SELECT dn.date_of_birth, d.collected_at FROM donor dn "
            "JOIN donation d ON d.donor_id = dn.id "
            "WHERE dn.date_of_birth IS NOT NULL"
        )
    ).all()

    minimum = int(config.get("donor_eligibility.age_years_min"))
    maximum = int(config.get("donor_eligibility.age_years_max"))

    outside = 0

    for born, collected in rows:
        age = _exact_age(
            date.fromisoformat(str(born)[:10]),
            date.fromisoformat(str(collected)[:10]),
        )

        if not (minimum <= age <= maximum):
            outside += 1

    assert outside == 0, (
        f"{outside:,} donations came from donors outside {minimum}-{maximum}"
    )


def test_the_register_still_contains_donors_outside_the_range(db):
    """They must exist — people age out, and a 17-year-old may pre-register — or
    the age rule above is never actually exercised by this data."""

    from datetime import date

    rows = db.execute(
        text("SELECT date_of_birth FROM donor WHERE date_of_birth IS NOT NULL")
    ).all()

    today = DEMO_DATETIME.date()
    minimum = int(config.get("donor_eligibility.age_years_min"))
    maximum = int(config.get("donor_eligibility.age_years_max"))

    outside = sum(
        1
        for (born,) in rows
        if not (
            minimum
            <= _exact_age(date.fromisoformat(str(born)[:10]), today)
            <= maximum
        )
    )

    assert outside > 0, "no donor is outside the age range, so the rule is untested"


# ------------------------------------------------------- lifecycle ordering


def test_no_unit_left_the_shelf_before_it_was_collected(db):
    """`collected_at` derives from the expiry index while a terminal event comes
    from an independent event day, and both draw a random hour — so a unit
    collected at 13:07 could be issued at 11:10 the same day. 22,320 transfused
    units were dated that way, and every turnaround statistic over them was
    wrong."""

    issued = db.scalar(
        text(
            "SELECT COUNT(*) FROM blood_unit "
            "WHERE issued_at IS NOT NULL AND issued_at < collected_at"
        )
    )
    discarded = db.scalar(
        text(
            "SELECT COUNT(*) FROM blood_unit "
            "WHERE discarded_at IS NOT NULL AND discarded_at < collected_at"
        )
    )

    assert issued == 0, f"{issued:,} units were issued before they were collected"
    assert discarded == 0, f"{discarded:,} units were discarded before they existed"


def test_no_unit_predates_the_donation_it_came_from(db):
    """One bag, one collection moment. A unit drawn after its own donation was
    recorded means the donation's timestamp does not cover its own contents, and
    every event derived from it lands too early."""

    ahead = db.scalar(
        text(
            "SELECT COUNT(*) FROM blood_unit u JOIN donation d ON d.id = u.donation_id "
            "WHERE u.collected_at > d.collected_at"
        )
    )

    assert ahead == 0, f"{ahead:,} units were collected after their own donation"


def test_every_expired_unit_records_why(db):
    """An expired unit with no reason hides its wastage from any reason-keyed
    query, which is how a fifth of expiry loss went missing from the reports."""

    unexplained = db.scalar(
        text(
            "SELECT COUNT(*) FROM blood_unit "
            "WHERE status = 'EXPIRED' AND (discard_reason IS NULL OR discarded_at IS NULL)"
        )
    )

    assert unexplained == 0, f"{unexplained:,} expired units record no discard reason"


# --------------------------------------------------------- collection volume


def test_nobody_below_the_absolute_weight_floor_was_bled(db):
    """Below the reduced-volume floor the SOP says do not collect at all. The
    generator hardcoded 450 mL and sampled weight independently, so it recorded
    2,218 donations from donors who could not lawfully give one."""

    from core import config

    floor = float(config.get("donor_eligibility.weight_kg_min_reduced_volume"))

    bled = db.scalar(
        text(
            "SELECT COUNT(*) FROM donation d "
            "JOIN donor_screening s ON s.id = d.screening_id "
            "WHERE s.weight_kg < :floor"
        ),
        {"floor": floor},
    )

    assert bled == 0, f"{bled:,} donations came from donors under {floor} kg"


def test_no_donation_exceeds_the_volume_the_donors_weight_permits(db):
    """The two-step ladder, enforced against the recorded weight rather than
    assumed. A proportional draw would silently change the blood-to-anticoagulant
    ratio, which is why core.eligibility implements a ladder and not a formula."""

    from core import config, eligibility

    full_floor = float(config.get("donor_eligibility.weight_kg_min_full_volume"))

    over = db.scalar(
        text(
            "SELECT COUNT(*) FROM donation d "
            "JOIN donor_screening s ON s.id = d.screening_id "
            "WHERE d.donation_type <> 'APHERESIS' "
            "  AND s.weight_kg < :full AND d.volume_ml > :reduced"
        ),
        {
            "full": full_floor,
            "reduced": eligibility.collection_volume_for(full_floor - 1),
        },
    )

    assert over == 0, f"{over:,} donors under {full_floor} kg gave a full-volume unit"


def test_an_underweight_deferral_records_an_underweight_donor(db):
    from core import config

    floor = float(config.get("donor_eligibility.weight_kg_min_reduced_volume"))

    contradictory = db.scalar(
        text(
            "SELECT COUNT(*) FROM donor_screening "
            "WHERE deferral_reason_code = 'UNDERWEIGHT' AND weight_kg >= :floor"
        ),
        {"floor": floor},
    )

    assert contradictory == 0


# ------------------------------------------------------- rule applicability


def test_no_donor_is_deferred_for_a_rule_that_cannot_apply_to_them(db):
    """Some deferral rules only ever apply to one sex.

    The generator drew a reason before it knew whose it was, so 241 male donors
    were recorded as deferred for pregnancy, post-delivery or breastfeeding.
    That is visible on the clinical sign-off queue, where it discredits every
    other number on the page.

    Applicability is stated by the rule in config, not inferred from the reason
    code, so this test reads the same source the generator does.
    """

    rules = config.get("deferral_rules") or {}
    restricted = {
        code: str(rule["applies_to_sex"]).upper()
        for code, rule in rules.items()
        if rule.get("applies_to_sex")
    }

    assert restricted, "no rule declares applies_to_sex; this test proves nothing"

    for code, only_sex in restricted.items():
        wrong = db.scalar(
            text(
                "SELECT COUNT(*) FROM donor_deferral f "
                "JOIN donor d ON d.id = f.donor_id "
                "WHERE f.reason_code = :code AND UPPER(d.gender) <> :sex"
            ),
            {"code": code, "sex": only_sex},
        )

        assert wrong == 0, (
            f"{wrong:,} donors of the wrong sex are deferred under {code}, "
            f"which applies only to {only_sex}"
        )


def test_every_deferral_reason_is_a_configured_rule(db):
    """A reason code with no rule behind it cannot be reviewed, lifted or
    explained to the donor."""

    known = set(config.get("deferral_rules") or {})

    unknown = db.execute(
        text(
            "SELECT DISTINCT reason_code FROM donor_deferral "
            "WHERE reason_code NOT LIKE 'TTI_%'"
        )
    ).all()

    orphans = sorted({row[0] for row in unknown} - known)

    assert not orphans, f"deferral reasons with no configured rule: {orphans}"


# ------------------------------------------------------- the quarantine shelf


def test_donations_awaiting_results_have_no_results(db):
    """A donation still on the quarantine shelf must not carry a screening
    result, or the worklist is claiming work that is already done."""

    contradictory = db.scalar(
        text(
            "SELECT COUNT(*) FROM donation d WHERE d.status = 'COLLECTED' "
            "AND EXISTS (SELECT 1 FROM donation_test t "
            "            WHERE t.donation_id = d.id AND t.test_group = 'TTI')"
        )
    )

    assert contradictory == 0


def test_units_awaiting_results_are_quarantined_not_available(db):
    """The whole point of the shelf. An untested bag must not be issuable."""

    issuable = db.scalar(
        text(
            "SELECT COUNT(*) FROM blood_unit u "
            "JOIN donation d ON d.id = u.donation_id "
            "WHERE d.status = 'COLLECTED' AND u.status = 'AVAILABLE'"
        )
    )

    assert issuable == 0, f"{issuable:,} untested units are counted as available"

    wrong_screening = db.scalar(
        text(
            "SELECT COUNT(*) FROM blood_unit u "
            "JOIN donation d ON d.id = u.donation_id "
            "WHERE d.status = 'COLLECTED' AND u.screening_status = 'PASSED'"
        )
    )

    assert wrong_screening == 0, (
        f"{wrong_screening:,} untested units claim to have passed screening"
    )


def test_the_full_panel_rule_applies_only_to_donations_the_lab_has_reached(db):
    """Every RELEASED or QUARANTINED donation ran the full panel. A COLLECTED one
    has not been tested yet, and requiring results from it would be requiring a
    result that does not exist."""

    required = list(config.get("tti_panel.required_tests"))

    if not config.get("tti_panel.malaria_screening_enabled"):
        required = [code for code in required if code != "MALARIA"]

    short = db.scalar(
        text(
            "SELECT COUNT(*) FROM ("
            "  SELECT t.donation_id, COUNT(DISTINCT t.test_code) AS n "
            "  FROM donation_test t "
            "  JOIN donation d ON d.id = t.donation_id "
            "  WHERE t.test_group = 'TTI' AND d.status IN ('RELEASED', 'QUARANTINED') "
            "  GROUP BY t.donation_id) WHERE n < :needed"
        ),
        {"needed": len(required)},
    )

    assert short == 0, f"{short:,} processed donations are missing part of the panel"


def test_the_quarantine_shelf_is_not_empty(db):
    """A bank with nothing awaiting results has either just been rebuilt or is
    not modelling the lab's batch schedule. Either way the lab worklist would
    have nothing to show, which hid a whole module."""

    waiting = db.scalar(
        text("SELECT COUNT(*) FROM donation WHERE status IN ('COLLECTED', 'TESTING')")
    )

    assert waiting > 0, (
        "nothing is awaiting lab results, so the quarantine shelf does not exist"
    )
