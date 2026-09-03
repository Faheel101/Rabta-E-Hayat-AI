"""Retro-fit the vein-to-vein chain onto units that already exist.

The unit inventory was generated first, from demand — which is the right way
round, because the demand signal is what the forecasting engines are judged on.
But it left 138,096 units with no provenance: no donor, no screening, no
donation, no test result, no processing record. A blood unit you cannot trace
back to a donor is not a blood unit, it is a row.

This module builds that history backwards. It reads the units, assembles them
into plausible donations, invents the donor who gave each one, and writes the
screening, test panel and component-production records that must have existed
for those units to be sitting on a shelf.

Three things it deliberately does NOT fake:

1. **Donation intervals.** A donor cannot give whole blood twice inside 90 days
   (120 for women). With 108,819 units in the 45-day window, reusing the 5,000
   existing donors would mean 14 donations each — clinically impossible, and it
   would make every interval check in `core.eligibility` untestable. So the
   donor pool grows to match. That is not a workaround; it is what Pakistan's
   register actually looks like, because 72% of donations are one-time
   replacement donors who never return.

2. **Reactive units.** A TTI-reactive donation is quarantined, never
   released, produces no components, and every unit traced to it is discarded.
   Because the inventory was generated before this history existed, reactivity
   can only be assigned to donations whose units never left the shelf —
   otherwise the records would assert that a patient was transfused with
   infected blood. Where the target rate cannot be met inside that constraint,
   the run says so rather than closing the gap.

3. **Who signed what.** Collection is attributed to a phlebotomist, testing to
   one lab technologist and verification to a different one. Where a facility
   has no second technologist the release is left unverified rather than
   self-verified, which is the honest representation of a real gap.

The window is bounded: `--window-days` (default 45) back from the demo instant
is treated as the system go-live. Units older than that keep their identifiers
but carry no donation record, exactly as they would after a real migration.
"""

from __future__ import annotations

import argparse
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import numpy as np
from sqlalchemy import bindparam, delete, func, select

from core import config, eligibility
from core.clock import DEMO_DATETIME, as_utc
from datagen.donors import (
    AGE_BAND_RANGE,
    DONOR_TYPE_WEIGHTS,
    MALE_SHARE,
    choose,
    hash_cnic,
    make_cnic,
    make_name,
    make_phone,
)
from db.models import (
    BloodGroup,
    BloodUnit,
    Component,
    ComponentProduction,
    Donation,
    DonationSession,
    DonationTest,
    Donor,
    DonorDeferral,
    DonorScreening,
    Facility,
    UserAccount,
)
from db.session import SessionLocal, init_db

SEED = config.SEED

# Copper sulphate is the method the Punjab SOP specifies, and it gives one
# threshold for everyone rather than a sex-dependent pair.
HAEMOGLOBIN_MIN = float(config.get("donor_eligibility.haemoglobin.cuso4_gdl_all"))

# Donors this module creates carry their own code prefix so a re-run can
# remove exactly what the previous run added. Seed donors (PK-D-*) are
# migrated register entries and are never touched.
GENERATED_DONOR_PREFIX = "D-"

# Below this a donor is turned away outright; between it and the full-volume
# floor only a reduced draw is permitted. core.eligibility owns the ladder;
# this is only the resampling bound.
WEIGHT_MIN_REDUCED = float(
    config.get("donor_eligibility.weight_kg_min_reduced_volume")
)

# An apheresis platelet collection returns the red cells, so donor weight
# does not set the product volume the way it does for a whole blood draw.
APHERESIS_VOLUME_ML = 250

# The register accepts these ages. Donors outside the range may exist — people
# age out, and a 17-year-old can pre-register — but none of them is ever bled.
AGE_MIN = int(config.get("donor_eligibility.age_years_min"))
AGE_MAX = int(config.get("donor_eligibility.age_years_max"))

# Which components a single collection can yield. A triple bag processed by the
# buffy-coat method gives red cells, platelets and plasma from one 450 mL
# donation; a double gives red cells and plasma. Cryoprecipitate is made from
# that donation's plasma rather than collected separately.
RECIPES = [
    ("WB_TRIPLE_BUFFY", ["PRBC", "PLT_RD", "FFP"]),
    ("WB_DOUBLE", ["PRBC", "FFP"]),
    ("WB_DOUBLE_CRYO", ["PRBC", "CRYO"]),
    ("WB_RBC_ONLY", ["PRBC"]),
]

# Donations that yield a single component and nothing else.
SOLO_COMPONENTS = {
    "WB": ("WHOLE_BLOOD_ONLY", "WHOLE_BLOOD"),
    "PLT_APH": ("APHERESIS_PLATELET", "APHERESIS"),
}

BAG_TYPES = {
    "WB_TRIPLE_BUFFY": "TRIPLE",
    "WB_DOUBLE": "DOUBLE",
    "WB_DOUBLE_CRYO": "DOUBLE",
    "WB_DOUBLE_PLATELET": "DOUBLE",
    "WB_RBC_ONLY": "SINGLE",
    "WHOLE_BLOOD_ONLY": "SINGLE",
    "APHERESIS_PLATELET": "APHERESIS_KIT",
}

# Screening outcomes. Roughly one donor in eight is turned away, dominated by
# low haemoglobin — which is what you would expect in a population with high
# background anaemia.
# Screening outcomes. Roughly one donor in eight is turned away, dominated by
# low haemoglobin — which is what you would expect in a population with high
# background anaemia.
DEFERRAL_SHARE = 0.125

# How often each reason comes up. This is data-generation tuning, NOT clinical
# policy: which rules exist, what kind each is and how long it lasts all come
# from config/network.yaml's deferral_rules, which is the reviewable copy.
#
# This module used to keep its own eight-reason table complete with durations,
# and it contradicted the config on four of them — low haemoglobin deferred for
# 90 days here and 30 there. The generator's copy won, so the clinical policy
# anyone would read was not the one in force.
#
# Reasons named here get the stated share. Every other configured rule splits
# what is left, so all thirty-two are exercised — including the contested ones
# that need clinical sign-off, which would otherwise never appear in the data.
DEFERRAL_FREQUENCY = {
    "LOW_HAEMOGLOBIN": 0.40,
    "RECENT_DONATION": 0.12,
    "UNDERWEIGHT": 0.08,
    "BP_OUT_OF_RANGE": 0.06,
    "ACUTE_ILLNESS_FEVER": 0.05,
    "PULSE_OUT_OF_RANGE": 0.03,
}

# What share of deferrals falls to everything else, spread evenly.
DEFERRAL_TAIL_SHARE = 0.25


def deferral_policy() -> dict[str, dict]:
    """The configured rules, keyed by reason code.

    Each value carries `kind` (TIMED / CONDITIONAL / PERMANENT), an optional
    `days`, and a `confidence`. The kind is the part that matters: a CONDITIONAL
    deferral has no end date at all, and encoding it as zero days would let any
    `today >= deferred_until` check score the donor eligible immediately — the
    exact mistake core/eligibility.py's enum exists to prevent.
    """

    return dict(config.get("deferral_rules") or {})


def applicable_reasons(
    rules: dict, codes: list[str], weights, sex: str
) -> tuple[list[str], "np.ndarray"]:
    """Narrow the deferral distribution to rules that can apply to this donor.

    Some rules only ever apply to one sex. Drawing without checking recorded 241
    male donors as deferred for pregnancy, post-delivery or breastfeeding — which
    is visible on the clinical sign-off queue and discredits every other number
    on the page.

    The rule states its own applicability in config; this does not infer it from
    the reason code.
    """

    keep = [
        index
        for index, code in enumerate(codes)
        if not rules[code].get("applies_to_sex")
        or str(rules[code]["applies_to_sex"]).upper() == str(sex).upper()
    ]

    narrowed = np.array([weights[index] for index in keep], dtype=float)

    return [codes[index] for index in keep], narrowed / narrowed.sum()


def deferral_days_for(rule: dict, sex: str) -> int:
    """How long a TIMED deferral lasts, for this donor.

    Some durations are sex-dependent — the interval between whole blood
    donations is 90 days for men and 120 for women — so the rule carries
    `days_male` and `days_female` instead of a single `days`. Reading only
    `days` returned nothing and silently demoted a rule with a definite end date
    into a conditional one with none.
    """

    if "days" in rule and rule.get("days"):
        return int(rule["days"])

    key = "days_female" if str(sex).upper() == "FEMALE" else "days_male"

    return int(rule.get(key) or 0)


def deferral_distribution(rules: dict[str, dict]) -> tuple[list[str], np.ndarray]:
    """Reason codes and their sampling weights, summing to one."""

    named = [code for code in DEFERRAL_FREQUENCY if code in rules]
    others = [code for code in rules if code not in DEFERRAL_FREQUENCY]

    weights = [DEFERRAL_FREQUENCY[code] for code in named]

    if others:
        share = DEFERRAL_TAIL_SHARE / len(others)
        weights.extend([share] * len(others))
    elif named:
        # No tail: give the named reasons the whole distribution.
        weights = [w / sum(weights) for w in weights]

    codes = named + others
    array = np.array(weights, dtype=float)

    return codes, array / array.sum()


# Transfusion-transmissible infection prevalence. These are the reactive rates
# seen in Pakistani donor populations; HCV is the dominant one by a wide margin.
# Screening reactivity is higher than true prevalence because a rapid/ELISA
# screen catches false positives that confirmatory testing would clear.
TTI_REACTIVE_RATE = {
    "HIV": 0.0009,
    "HBSAG": 0.021,
    "HCV": 0.032,
    "SYPHILIS": 0.011,
    "MALARIA": 0.004,
}

# How recently a bag has to have been collected for its results to still be
# outstanding. A donation drawn inside this window sits on the quarantine shelf
# with its panel pending, which is what a real bank looks like at any moment and
# what gives the lab worklist something to work on.
# The lab reads plates twice a day: a morning run and an afternoon one.
#
# The morning slot sits AFTER the demo instant on purpose. At 08:00 the
# previous evening's collections have not been read yet — they are on the
# quarantine shelf waiting for the 09:00 plate. That is the ordinary state of
# a blood bank at eight in the morning, and it is what gives the lab worklist
# something real to show.
RUN_SLOTS_UTC = (9, 15)

# The confirmatory tier. A reactive screen protects the supply immediately by
# discarding the unit; only a confirmatory assay decides what happens to the
# person. Rates and methods come from config so the clinical policy is in one
# reviewable place rather than in this file.
CONFIRMATORY_ENABLED = bool(config.get("tti_panel.confirmatory.enabled"))
CONFIRMATION_RATE = dict(config.get("tti_panel.confirmatory.confirmation_rate") or {})
CONFIRMATORY_METHOD = dict(config.get("tti_panel.confirmatory.method") or {})
UNCONFIRMED_DEFERRAL_DAYS = int(
    config.get("tti_panel.confirmatory.unconfirmed_deferral_days") or 180
)

TEST_METHOD = {
    "HIV": "ELISA (4th generation)",
    "HBSAG": "ELISA",
    "HCV": "ELISA (3rd generation)",
    "SYPHILIS": "RPR",
    "MALARIA": "ICT (antigen)",
}

# Sessions. A facility runs its own in-house bench most days; outreach camps at
# mosques, universities and factories run on top of that.
CAMP_VENUES = [
    "Jamia Masjid", "Government College", "University Campus",
    "Textile Mill", "District Bar Association", "Rescue 1122 Station",
    "Chamber of Commerce", "Municipal Corporation Hall", "Cadet College",
    "Railway Colony", "Steel Works", "Teachers Training Institute",
]

CAMP_ORGANISERS = [
    "Pakistan Red Crescent Society", "Fatimid Foundation",
    "Rotary Club", "Edhi Foundation", "Al-Khidmat Foundation",
    "Thalassaemia Federation", "University Blood Donors Society",
]


def _next_run_slot(collected, rng) -> datetime:
    """When the plate carrying this sample was actually read.

    Two runs a day, morning and afternoon, plus an hour of sample prep. A
    donation that misses the afternoon plate waits for tomorrow morning — which
    is the ordinary case for anything collected after about three o'clock.
    """

    ready = collected + timedelta(hours=1)

    for hour in RUN_SLOTS_UTC:
        slot = ready.replace(hour=hour, minute=0, second=0, microsecond=0)

        if slot >= ready:
            return slot + timedelta(minutes=float(rng.uniform(0, 150)))

    tomorrow = ready + timedelta(days=1)

    return tomorrow.replace(
        hour=RUN_SLOTS_UTC[0], minute=0, second=0, microsecond=0
    ) + timedelta(minutes=float(rng.uniform(0, 150)))


def _interval_days(gender: str) -> int:
    """Minimum days between whole blood donations, from config."""

    key = "female" if gender == "FEMALE" else "male"
    return int(config.get(f"donation_interval.whole_blood_days_{key}"))


def _birth_date(rng, *, age: int, on: date) -> date:
    """A birth date that makes someone exactly `age` years old on `on`.

    The birthday falls in the year before `on`, so it has already passed by the
    donation date whatever the month. Choosing a month freely inside
    `on.year - age` made anyone with a later birthday a year younger than
    intended — enough to put 840 donors under the minimum age.
    """

    birthday_year = on.year - age - 1
    month = int(rng.integers(1, 13))
    day = int(rng.integers(1, 29))
    born = date(birthday_year, month, day)

    # If that lands more than a year before their birthday, pull it forward into
    # the intended year — anything on or before `on`'s month/day is already past.
    if (month, day) <= (on.month, on.day):
        born = date(on.year - age, month, day)

    return born


class DonorPool:
    """Hands out a donor for each donation, honouring the interval rule.

    Existing donors are reused where their last recorded donation is far enough
    in the past. When none qualifies — which is most of the time, because the
    window is shorter than the interval — a new donor is minted. The pool is
    keyed by blood group because the donation's group is already fixed by the
    unit it produced.
    """

    def __init__(self, session, rng: np.random.Generator):
        self.session = session
        self.rng = rng
        self.new_donors: list[dict] = []

        # donor id -> last donation date, seeded from the existing register
        self.last_donation: dict[str, date] = {}
        self.by_group: dict[int, list[str]] = defaultdict(list)
        self.gender: dict[str, str] = {}
        self.born: dict[str, date] = {}
        # Donors who must never be handed out again — permanently deferred,
        # or confirmed positive on a transfusion-transmissible infection.
        self.barred: set[str] = set()

        rows = session.execute(
            select(
                Donor.id,
                Donor.blood_group_id,
                Donor.gender,
                Donor.last_donation_at,
                Donor.date_of_birth,
            ).where(Donor.is_permanently_deferred.is_(False))
        ).all()

        for donor_id, group_id, gender, last, born in rows:
            if group_id is None:
                continue

            self.by_group[group_id].append(donor_id)
            self.gender[donor_id] = gender or "MALE"

            if born is not None:
                self.born[donor_id] = born

            last = as_utc(last)

            if last is not None:
                self.last_donation[donor_id] = last.date()

        # Round-robin cursor per group so reuse spreads across the register
        # instead of hammering whoever happens to sort first.
        self._cursor: dict[int, int] = defaultdict(int)

        # Codes continue past the migrated register rather than restarting at
        # 1, so a generated code can never collide with a seed one.
        self._counter = int(
            session.scalar(
                select(func.count())
                .select_from(Donor)
                .where(Donor.donor_code.not_like(f"{GENERATED_DONOR_PREFIX}%"))
            )
            or 0
        )

    def mint(self, group_id: int, facility: Facility, when: date) -> str:
        rng = self.rng
        is_male = bool(rng.random() < MALE_SHARE)
        full_name = make_name(rng, is_male)

        band = str(rng.choice(list(AGE_BAND_RANGE)))
        low, high = AGE_BAND_RANGE[band]
        age = int(rng.integers(max(low, AGE_MIN), min(high, AGE_MAX) + 1))

        cnic = make_cnic(rng)
        donor_id = str(uuid.uuid4())
        self._counter += 1

        self.new_donors.append(
            {
                "id": donor_id,
                "donor_code": f"{GENERATED_DONOR_PREFIX}{self._counter:06d}",
                "organization_id": facility.organization_id,
                "registered_facility_id": facility.id,
                "full_name": full_name,
                "cnic_hash": hash_cnic(cnic),
                "cnic_last4": cnic[-4:],
                "phone": make_phone(rng),
                "gender": "MALE" if is_male else "FEMALE",
                # Born far enough back that they are exactly `age` ON the
                # donation date. Picking a month and day freely inside
                # `when.year - age` made anyone whose birthday had not yet passed
                # a year younger than intended, which is where 840 under-18
                # donors came from.
                "date_of_birth": _birth_date(rng, age=age, on=when),
                "address": f"{facility.district}, {facility.division}",
                "blood_group_id": group_id,
                "blood_group_confirmed": True,
                "city": facility.district,
                "district": facility.district,
                "age_band": band,
                "donor_type": choose(rng, DONOR_TYPE_WEIGHTS),
                "availability_status": "AVAILABLE",
                "first_donation_at": None,
                "last_donation_at": None,
                "total_donations": 0,
                "deferred_until": None,
                "is_permanently_deferred": False,
                # Most replacement donors decline follow-up contact; that low
                # consent rate is why the recall panel is small.
                "consent_contact": bool(rng.random() < 0.42),
                "is_active": True,
                "created_at": datetime.combine(
                    when, datetime.min.time(), tzinfo=timezone.utc
                ),
            }
        )

        self.by_group[group_id].append(donor_id)
        self.gender[donor_id] = "MALE" if is_male else "FEMALE"
        self.born[donor_id] = self.new_donors[-1]["date_of_birth"]

        return donor_id

    def _age_ok(self, donor_id: str, when: date) -> bool:
        """Is this donor inside the accepted age range on the donation day?

        A donor with no recorded birth date is refused rather than waved through:
        an unknown must never produce a more permissive answer than a known.
        """

        born = self.born.get(donor_id)

        if born is None:
            return False

        years = when.year - born.year

        if (when.month, when.day) < (born.month, born.day):
            years -= 1

        return AGE_MIN <= years <= AGE_MAX

    def take(self, group_id: int, facility: Facility, when: date) -> str:
        """A donor eligible to give on `when`, reused if possible.

        Eligible means: inside the donation interval, inside the accepted age
        range on the day, and not permanently deferred. Donors outside the age
        range stay in the register — people do age out, and a 17-year-old may
        pre-register — they simply never get bled.
        """

        candidates = self.by_group.get(group_id) or []

        # Try a bounded number of existing donors rather than scanning the whole
        # register for every one of ~70,000 donations.
        for _ in range(min(24, len(candidates))):
            index = self._cursor[group_id] % len(candidates)
            self._cursor[group_id] += 1
            donor_id = candidates[index]

            if donor_id in self.barred:
                continue

            if not self._age_ok(donor_id, when):
                continue

            last = self.last_donation.get(donor_id)
            interval = _interval_days(self.gender.get(donor_id, "MALE"))

            if last is None or (when - last).days >= interval:
                self.last_donation[donor_id] = when
                return donor_id

        donor_id = self.mint(group_id, facility, when)
        self.last_donation[donor_id] = when

        return donor_id


def _staff_by_facility(session) -> dict[str, dict[str, list[str]]]:
    """Who can sign for what, per facility.

    Falls back to the facility's officers where no dedicated bench staff exist,
    because a small district bank genuinely has one person doing both jobs. The
    two-person release rule then simply cannot be satisfied there, and the
    records say so rather than pretending otherwise.
    """

    staff: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: {"collect": [], "test": []}
    )

    rows = session.execute(
        select(UserAccount.full_name, UserAccount.role, UserAccount.facility_id)
        .where(UserAccount.facility_id.is_not(None))
        .where(UserAccount.is_active.is_(True))
    ).all()

    for name, role, facility_id in rows:
        if role in ("PHLEBOTOMIST", "BLOOD_BANK_OFFICER", "RBC_COORDINATOR"):
            staff[facility_id]["collect"].append(name)

        if role in ("LAB_TECHNOLOGIST", "BLOOD_BANK_OFFICER", "RBC_COORDINATOR"):
            staff[facility_id]["test"].append(name)

    return staff


def _session_rows(rng, facility: Facility, day: date, unit_count: int) -> list[dict]:
    """The collection sessions a facility ran on a given day."""

    rows = [
        {
            "id": str(uuid.uuid4()),
            "session_code": f"{facility.code}-{day:%y%m%d}-BENCH",
            "facility_id": facility.id,
            "organization_id": facility.organization_id,
            "session_type": "IN_HOUSE",
            "name": f"{facility.name_en} donor bench",
            "venue": facility.name_en,
            "district": facility.district,
            "latitude": facility.latitude,
            "longitude": facility.longitude,
            "scheduled_date": day,
            "opened_at": datetime.combine(
                day, datetime.min.time(), tzinfo=timezone.utc
            ) + timedelta(hours=3),
            "closed_at": datetime.combine(
                day, datetime.min.time(), tzinfo=timezone.utc
            ) + timedelta(hours=15),
            "target_units": max(1, int(unit_count * 0.7)),
            "status": "CLOSED",
            "created_at": DEMO_DATETIME,
        }
    ]

    # Outreach camps: bigger centres run them most days, small banks rarely.
    camp_chance = 0.55 if facility.facility_type == "RBC" else 0.12

    if unit_count >= 20 and rng.random() < camp_chance:
        venue = str(rng.choice(CAMP_VENUES))
        rows.append(
            {
                "id": str(uuid.uuid4()),
                "session_code": f"{facility.code}-{day:%y%m%d}-CAMP",
                "facility_id": facility.id,
                "organization_id": facility.organization_id,
                "session_type": "OUTREACH_CAMP",
                "name": f"{venue} blood drive",
                "venue": f"{venue}, {facility.district}",
                "district": facility.district,
                "latitude": facility.latitude,
                "longitude": facility.longitude,
                "scheduled_date": day,
                "opened_at": datetime.combine(
                    day, datetime.min.time(), tzinfo=timezone.utc
                ) + timedelta(hours=4),
                "closed_at": datetime.combine(
                    day, datetime.min.time(), tzinfo=timezone.utc
                ) + timedelta(hours=11),
                "target_units": int(rng.integers(30, 121)),
                "status": "CLOSED",
                "organiser": str(rng.choice(CAMP_ORGANISERS)),
                "contact_phone": make_phone(rng),
                "created_at": DEMO_DATETIME,
            }
        )

    return rows


def _bag_type_for(recipe: str, members) -> str:
    """The kit a bag yielding this many products must have been collected into.

    Derived from the count rather than looked up by recipe name. A lookup needs
    an entry for every combination and silently defaults when one is missing —
    which recorded 1,636 three-unit bags as SINGLE, a kit that yields one.
    """

    if recipe == "APHERESIS_PLATELET":
        return "APHERESIS_KIT"

    return {1: "SINGLE", 2: "DOUBLE", 3: "TRIPLE"}.get(len(members), "TRIPLE")


def _recipe_for(members, component_code: dict) -> str:
    """Name the recipe after what the bag actually produced."""

    codes = {component_code[member.component_id] for member in members}

    if codes == {"PRBC", "PLT_RD", "FFP"}:
        return "WB_TRIPLE_BUFFY"

    if codes == {"PRBC", "FFP"}:
        return "WB_DOUBLE"

    if codes == {"PRBC", "CRYO"}:
        return "WB_DOUBLE_CRYO"

    if codes == {"PRBC", "PLT_RD"}:
        return "WB_DOUBLE_PLATELET"

    if codes == {"PRBC"}:
        return "WB_RBC_ONLY"

    return "WB_COMPONENT_ONLY"


def _assemble(
    rng, units_by_component: dict[str, list], component_code: dict
) -> list[tuple[str, list]]:
    """Group a day's units into the donations that must have produced them.

    Greedy: every red cell unit anchors a whole blood donation, which then picks
    up a platelet and a plasma unit from the same day if any are going spare.
    Leftovers become their own donations — a real bank does end up with orphan
    plasma when the red cells from that bag were discarded at processing.
    """

    donations: list[tuple[str, list]] = []
    pools = {code: list(units) for code, units in units_by_component.items()}

    for code, (recipe, _) in SOLO_COMPONENTS.items():
        for unit in pools.pop(code, []):
            donations.append((recipe, [unit]))

    reds = pools.pop("PRBC", [])
    platelets = pools.pop("PLT_RD", [])
    plasma = pools.pop("FFP", [])
    cryo = pools.pop("CRYO", [])

    for red in reds:
        members = [red]

        if platelets and rng.random() < 0.85:
            members.append(platelets.pop())

        if plasma and rng.random() < 0.9:
            members.append(plasma.pop())
        elif cryo and rng.random() < 0.9:
            members.append(cryo.pop())

        # The recipe is derived from what the bag ACTUALLY yielded, not set
        # part-way through and then left behind. Setting it when the platelet
        # was added and never revising it produced 7,940 donations recorded as
        # triple bags holding two units — which then reads downstream as a
        # third of every triple bag losing a component.
        donations.append((_recipe_for(members, component_code), members))

    # Anything with no red cell partner still came from somebody.
    for leftover in (platelets, plasma, cryo):
        for unit in leftover:
            donations.append(("WB_COMPONENT_ONLY", [unit]))

    return donations


def build(session, window_days: int, rng: np.random.Generator) -> dict:
    cutoff = DEMO_DATETIME - timedelta(days=window_days)

    facilities = {f.id: f for f in session.scalars(select(Facility)).all()}
    component_code = {
        c.id: c.code for c in session.scalars(select(Component)).all()
    }
    group_code = {
        g.id: g.code for g in session.scalars(select(BloodGroup)).all()
    }

    staff = _staff_by_facility(session)
    pool = DonorPool(session, rng)

    units = session.execute(
        select(
            BloodUnit.id,
            BloodUnit.din,
            BloodUnit.facility_id,
            BloodUnit.component_id,
            BloodUnit.blood_group_id,
            BloodUnit.collected_at,
            BloodUnit.status,
        )
        .where(BloodUnit.collected_at >= cutoff)
        .order_by(BloodUnit.collected_at)
    ).all()

    print(f"  units in window: {len(units):,}")

    # (facility, day) -> blood group -> component code -> units
    #
    # Blood group is part of the key, not a detail inside it. One bag yields one
    # donor's components, so every unit in a donation must carry that donor's
    # group. Bucketing on (facility, day) alone let the assembler pair an O+ red
    # cell with a B- plasma — 31% of donations, physically impossible, and it
    # breaks a lookback: recalling a reactive donation would pull units that came
    # from someone else entirely.
    buckets: dict[tuple[str, date], dict[int, dict[str, list]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )

    for unit in units:
        collected = as_utc(unit.collected_at)
        key = (unit.facility_id, collected.date())
        buckets[key][unit.blood_group_id][component_code[unit.component_id]].append(unit)

    sessions: list[dict] = []
    screenings: list[dict] = []
    donations: list[dict] = []
    tests: list[dict] = []
    productions: list[dict] = []
    unit_links: list[dict] = []
    discard_updates: list[dict] = []
    quarantine_updates: list[dict] = []
    pending: list[dict] = []
    deferrals: list[dict] = []
    donor_updates: dict[str, dict] = {}

    required_tests = list(config.get("tti_panel.required_tests"))

    if not config.get("tti_panel.malaria_screening_enabled"):
        required_tests = [t for t in required_tests if t != "MALARIA"]

    reactive_donations = 0
    deferred_screenings = 0
    confirmatory_enabled = CONFIRMATORY_ENABLED

    for (facility_id, day), by_group in sorted(
        buckets.items(), key=lambda item: (item[0][1], item[0][0])
    ):
        facility = facilities.get(facility_id)

        if facility is None:
            continue

        # Sessions belong to the facility's day, not to a blood group — a bench
        # runs once and bleeds whoever walks in.
        day_units = sum(
            len(units) for group in by_group.values() for units in group.values()
        )
        day_sessions = _session_rows(rng, facility, day, day_units)
        sessions.extend(day_sessions)

        collectors = staff[facility_id]["collect"] or ["Duty Phlebotomist"]
        testers = staff[facility_id]["test"] or ["Duty Technologist"]

        assembled = [
            (recipe, members)
            for group_id in sorted(by_group)
            for recipe, members in _assemble(rng, by_group[group_id], component_code)
        ]

        for recipe, members in assembled:
            anchor = members[0]
            group_id = anchor.blood_group_id

            # The LATEST member's timestamp, not the first one's. Units were
            # generated independently and carry their own collection times, so
            # taking the anchor's left siblings collected after it — and every
            # event derived from the donation (testing, release, discard) then
            # landed before those units were drawn. 1,749 units were discarded
            # before they existed.
            #
            # The max is the conservative choice: it keeps every derived event
            # after every member's own timestamp without touching expires_at,
            # which the expiry and inventory engines are calibrated against.
            collected = max(as_utc(member.collected_at) for member in members)

            donor_id = pool.take(group_id, facility, day)
            gender = pool.gender.get(donor_id, "MALE")

            chosen_session = day_sessions[
                int(rng.integers(0, len(day_sessions)))
            ]
            phlebotomist = str(rng.choice(collectors))

            # Screening happens shortly before the needle goes in.
            screened_at = collected - timedelta(minutes=int(rng.integers(8, 41)))
            screening_id = str(uuid.uuid4())

            # A donor who was bled must have passed the threshold. Clipping at
            # the floor rather than sampling freely is the difference between a
            # screening record that corroborates the donation and one that
            # contradicts it.
            haemoglobin = float(
                np.clip(
                    rng.normal(14.2 if gender == "MALE" else 13.1, 1.3),
                    HAEMOGLOBIN_MIN,
                    19.0,
                )
            )
            # Weight decides how much may be taken, and whether anything may be
            # taken at all. Sampling it freely and then hardcoding a 450 mL draw
            # recorded 2,218 donors below the 45 kg absolute floor as having been
            # bled, and 4,868 donors under 50 kg as giving a full unit the SOP
            # does not permit them to give.
            weight = float(
                np.clip(rng.normal(68 if gender == "MALE" else 58, 11), 42, 120)
            )

            permitted_volume = eligibility.collection_volume_for(weight)

            if permitted_volume is None:
                # Below the absolute floor. This donor is turned away, so the
                # unit in front of us cannot have come from them — resample onto
                # someone who could actually have given it.
                weight = float(
                    np.clip(
                        rng.normal(68 if gender == "MALE" else 58, 11),
                        WEIGHT_MIN_REDUCED,
                        120,
                    )
                )
                permitted_volume = eligibility.collection_volume_for(weight)

            screenings.append(
                {
                    "id": screening_id,
                    "donor_id": donor_id,
                    "session_id": chosen_session["id"],
                    "facility_id": facility_id,
                    "screened_at": screened_at,
                    "haemoglobin_g_dl": round(haemoglobin, 1),
                    "weight_kg": round(weight, 1),
                    "systolic_bp": int(np.clip(rng.normal(120, 11), 95, 165)),
                    "diastolic_bp": int(np.clip(rng.normal(78, 8), 58, 105)),
                    "pulse_bpm": int(np.clip(rng.normal(76, 9), 52, 110)),
                    "temperature_c": round(float(np.clip(rng.normal(36.7, 0.3), 35.8, 38.2)), 1),
                    "questionnaire_json": {"all_negative": True},
                    "outcome": "ACCEPTED",
                    "screened_by": phlebotomist,
                }
            )

            donation_id = str(uuid.uuid4())

            # Apheresis draws a fixed product volume; whole blood draws whatever
            # the donor's weight permits, from core.eligibility rather than from
            # a constant here.
            volume = (
                APHERESIS_VOLUME_ML
                if recipe == "APHERESIS_PLATELET"
                else permitted_volume
            )
            donation_type = SOLO_COMPONENTS.get(
                component_code[anchor.component_id], (None, "WHOLE_BLOOD")
            )[1]

            if recipe.startswith("WB"):
                donation_type = "WHOLE_BLOOD"

            # An adverse reaction in roughly one donation in 120; almost all are
            # vasovagal and mild.
            reaction = None

            if rng.random() < 0.008:
                reaction = str(
                    rng.choice(["VASOVAGAL_MILD", "HAEMATOMA", "VASOVAGAL_MODERATE"])
                )

            # Lab typing disagrees with the donor's stated group occasionally;
            # the unit is still usable, the register entry is what gets corrected.
            discrepancy = bool(rng.random() < 0.004)

            # One tester runs the panel, a different one verifies — that is the
            # whole reason the two roles exist.
            # Results come from the next scheduled plate, not from a stopwatch
            # started at the needle. A district lab batches its ELISA runs once
            # or twice a day, so a bag drawn in the evening waits for the morning
            # run — which is why there is always a quarantine shelf, and why the
            # last shift's collections are still outstanding at any given moment.
            tested_at = _next_run_slot(collected, rng)
            tested_by = str(rng.choice(testers))
            verifiers = [name for name in testers if name != tested_by]
            verified_by = str(rng.choice(verifiers)) if verifiers else None

            # Reactivity is NOT decided here. A reactive donation's units must
            # never have reached a patient, and 72% of the units in this window
            # are already recorded as transfused. Deciding now would manufacture
            # records saying a patient received a TTI-reactive unit. Which
            # donations may be reactive is settled in a second pass below, once
            # every unit's fate is known.
            pending.append(
                {
                    "donation": {
                        "id": donation_id,
                        "din": anchor.din,
                        "donor_id": donor_id,
                        "session_id": chosen_session["id"],
                        "screening_id": screening_id,
                        "facility_id": facility_id,
                        "is_directed": False,
                        "collected_at": collected,
                        "donation_type": donation_type,
                        "bag_type": _bag_type_for(recipe, members),
                        "anticoagulant": (
                            "CPDA-1" if recipe != "APHERESIS_PLATELET" else "ACD-A"
                        ),
                        "volume_ml": volume,
                        "typed_blood_group_id": group_id,
                        "grouping_discrepancy": discrepancy,
                        "adverse_reaction": reaction,
                        "phlebotomist": phlebotomist,
                        "created_at": collected,
                    },
                    "recipe": recipe,
                    "members": members,
                    "tested_at": tested_at,
                    "tested_by": tested_by,
                    "verified_by": verified_by,
                }
            )

    # ---------------------------------------------------------------- pass two
    #
    # A donation may only be TTI-reactive if none of its units ever left the
    # shelf. The target rate is then met by choosing reactive donations from
    # that eligible pool rather than by rolling the dice per donation, so the
    # published reactive rate stays right without inventing a transfusion of
    # infected blood.

    # Only these two. A unit already discarded for another reason would lose
    # that reason if it were overwritten here, and anything issued, reserved,
    # crossmatched or transfused has by definition reached a patient's side.
    NEVER_LEFT_THE_SHELF = {"AVAILABLE", "EXPIRED"}

    eligible = [
        index
        for index, record in enumerate(pending)
        if all(member.status in NEVER_LEFT_THE_SHELF for member in record["members"])
    ]

    # Probability that a donation trips at least one test in the panel.
    panel_rate = 1.0
    for code in required_tests:
        panel_rate *= 1.0 - TTI_REACTIVE_RATE[code]
    panel_rate = 1.0 - panel_rate

    wanted = int(round(len(pending) * panel_rate))
    reactive_indices: set[int] = set()

    if eligible:
        take = min(wanted, len(eligible))
        chosen = rng.choice(len(eligible), size=take, replace=False)
        reactive_indices = {eligible[int(i)] for i in np.atleast_1d(chosen)}

    # Per-test conditional rates, so which marker is reactive still follows the
    # real prevalence mix (HCV dominant) rather than being uniform.
    marker_weights = np.array(
        [TTI_REACTIVE_RATE[code] for code in required_tests], dtype=float
    )
    marker_weights = marker_weights / marker_weights.sum()

    awaiting_results = 0

    for index, record in enumerate(pending):
        donation = record["donation"]
        donation_id = donation["id"]
        members = record["members"]
        recipe = record["recipe"]
        tested_at = record["tested_at"]
        tested_by = record["tested_by"]
        verified_by = record["verified_by"]

        # Collected too recently for a result to exist yet. The donation stays
        # COLLECTED, its units stay quarantined, and the lab has work to do.
        if tested_at > DEMO_DATETIME:
            donation["status"] = "COLLECTED"
            donation["released_at"] = None
            donation["released_by"] = None
            donations.append(donation)
            awaiting_results += 1

            for member in members:
                unit_links.append({"b_id": member.id, "donation_id": donation_id})
                quarantine_updates.append(
                    {
                        "b_id": member.id,
                        "status": "QUARANTINE",
                        "screening_status": "PENDING",
                    }
                )

            continue

        is_reactive = index in reactive_indices

        if is_reactive:
            reactive_donations += 1
            # Which marker(s) tripped. Usually exactly one.
            primary = required_tests[int(rng.choice(len(required_tests), p=marker_weights))]
            reactive_codes = {primary}

            if rng.random() < 0.06:
                reactive_codes.add(
                    required_tests[int(rng.integers(0, len(required_tests)))]
                )
        else:
            reactive_codes = set()

        for test_code in required_tests:
            reactive = test_code in reactive_codes

            tests.append(
                {
                    "id": str(uuid.uuid4()),
                    "donation_id": donation_id,
                    "test_code": test_code,
                    "test_group": "TTI",
                    "method": TEST_METHOD[test_code],
                    "kit_lot": f"LOT-{int(rng.integers(10000, 99999))}",
                    "result": "REACTIVE" if reactive else "NON_REACTIVE",
                    "is_reactive": reactive,
                    "tested_at": tested_at,
                    "tested_by": tested_by,
                    # An unverified result is left unverified. A facility with
                    # one technologist cannot satisfy two-person release, and
                    # the record should show that rather than hide it.
                    "verified_at": (
                        tested_at + timedelta(minutes=int(rng.integers(20, 180)))
                        if verified_by
                        else None
                    ),
                    "verified_by": verified_by,
                }
            )

        donation["status"] = "QUARANTINED" if is_reactive else "RELEASED"
        donation["released_at"] = None if is_reactive else tested_at + timedelta(hours=1)
        donation["released_by"] = (
            None if is_reactive else (verified_by or tested_by)
        )
        donations.append(donation)

        if is_reactive:
            # The units exist because inventory was generated first. They are
            # discarded on the reactive result, which is what a real bank does
            # and what keeps the discard count honest. screening_status must move
            # too — leaving it PASSED makes a TTI lookback that filters on it
            # return nothing, which is the one query this data exists to answer.
            for member in members:
                discard_updates.append(
                    {
                        "b_id": member.id,
                        "status": "DISCARDED",
                        "screening_status": "FAILED",
                        "discard_reason": f"TTI_REACTIVE_{primary}",
                        "discarded_at": tested_at + timedelta(minutes=30),
                    }
                )

            # ------------------------------------------------ confirmatory tier
            #
            # The unit is already gone. What happens to the DONOR depends on a
            # confirmatory assay, because a reactive screen is not a diagnosis.
            if confirmatory_enabled:
                confirmed_markers = []

                # Confirmation takes days. For a donation screened in the last
                # week the result genuinely has not come back yet, and writing
                # one would be inventing a lab result that does not exist. Those
                # donors sit deferred-pending-confirmation, which is what a real
                # register looks like on any given morning.
                awaiting_confirmation = False

                for marker in sorted(reactive_codes):
                    confirms = bool(
                        rng.random() < CONFIRMATION_RATE.get(marker, 0.5)
                    )
                    confirmed_at = tested_at + timedelta(
                        days=float(rng.uniform(1.0, 6.0))
                    )

                    if confirmed_at > DEMO_DATETIME:
                        awaiting_confirmation = True
                        continue

                    # The confirmatory run is a second, different assay, and it
                    # is read by whoever did not run the screen where possible.
                    tests.append(
                        {
                            "id": str(uuid.uuid4()),
                            "donation_id": donation_id,
                            "test_code": marker,
                            "test_group": "TTI_CONFIRMATORY",
                            "method": CONFIRMATORY_METHOD.get(marker, "Confirmatory assay"),
                            "kit_lot": f"CONF-{int(rng.integers(10000, 99999))}",
                            "result": "POSITIVE" if confirms else "NEGATIVE",
                            "is_reactive": confirms,
                            "tested_at": confirmed_at,
                            "tested_by": verified_by or tested_by,
                            "verified_at": (
                                confirmed_at + timedelta(hours=float(rng.uniform(1, 20)))
                                if verified_by
                                else None
                            ),
                            "verified_by": tested_by if verified_by else None,
                            "notes": (
                                "Confirms the screening reactivity."
                                if confirms
                                else "Screening reactivity not confirmed; donor to be "
                                "re-tested before reinstatement."
                            ),
                        }
                    )

                    if confirms:
                        confirmed_markers.append(marker)

                donor_id = donation["donor_id"]

                # Nothing may be dated after the demo instant: a record dated in
                # the future is a record of something that has not happened.
                latest = min(tested_at + timedelta(days=6), DEMO_DATETIME)

                if awaiting_confirmation and not confirmed_markers:
                    # Screened reactive within the last few days; the
                    # confirmatory result is genuinely still outstanding. Deferred
                    # meanwhile, with no end date and no claim either way about
                    # whether this donor is infected.
                    pool.barred.add(donor_id)
                    deferrals.append(
                        {
                            "id": str(uuid.uuid4()),
                            "donor_id": donor_id,
                            "deferred_at": latest,
                            "deferred_until": None,
                            "is_permanent": False,
                            "reason_code": f"TTI_AWAITING_CONFIRMATION_{sorted(reactive_codes)[0]}",
                            "reason_note": (
                                "Screening reactive. Confirmatory testing is "
                                "outstanding; the donor is deferred until it "
                                "returns and no inference about infection status "
                                "may be drawn in the meantime."
                            ),
                            "recorded_by": verified_by or tested_by,
                        }
                    )
                    donor_updates[donor_id] = {
                        "d_id": donor_id,
                        "is_permanently_deferred": False,
                        "deferred_until": None,
                        "availability_status": "AWAITING_TTI_CONFIRMATION",
                    }
                elif confirmed_markers:
                    # Confirmed positive: permanent deferral, and the donor is
                    # never handed out again.
                    pool.barred.add(donor_id)
                    deferrals.append(
                        {
                            "id": str(uuid.uuid4()),
                            "donor_id": donor_id,
                            "deferred_at": latest,
                            "deferred_until": None,
                            "is_permanent": True,
                            "reason_code": f"TTI_CONFIRMED_{confirmed_markers[0]}",
                            "reason_note": (
                                "Confirmatory testing positive for "
                                + ", ".join(confirmed_markers)
                                + ". Donor to be notified and referred for "
                                "counselling and treatment."
                            ),
                            "recorded_by": verified_by or tested_by,
                        }
                    )
                    donor_updates[donor_id] = {
                        "d_id": donor_id,
                        "is_permanently_deferred": True,
                        "deferred_until": None,
                        "availability_status": "PERMANENTLY_DEFERRED",
                    }
                else:
                    # Not confirmed. The donor is not labelled infected, but nor
                    # are they waved straight back to the chair — something made
                    # the screen react.
                    until = (latest + timedelta(days=UNCONFIRMED_DEFERRAL_DAYS)).date()
                    pool.barred.add(donor_id)
                    deferrals.append(
                        {
                            "id": str(uuid.uuid4()),
                            "donor_id": donor_id,
                            "deferred_at": latest,
                            "deferred_until": until,
                            "is_permanent": False,
                            "reason_code": f"TTI_UNCONFIRMED_{sorted(reactive_codes)[0]}",
                            "reason_note": (
                                "Screening reactive, confirmatory testing negative. "
                                "Deferred pending a repeat test before reinstatement."
                            ),
                            "recorded_by": verified_by or tested_by,
                        }
                    )
                    donor_updates[donor_id] = {
                        "d_id": donor_id,
                        "is_permanently_deferred": False,
                        "deferred_until": until,
                        "availability_status": "TEMPORARILY_DEFERRED",
                    }
        else:
            productions.append(
                separation_record(
                    rng,
                    donation_id=donation_id,
                    facility_id=donation["facility_id"],
                    collected_at=donation["collected_at"],
                    produced_codes=[
                        component_code[member.component_id] for member in members
                    ],
                    bag_type=donation["bag_type"],
                    produced_by=tested_by,
                    tested_at=donation["collected_at"]
                    + timedelta(hours=float(rng.uniform(1.5, 7.0))),
                )
            )

        for member in members:
            unit_links.append({"b_id": member.id, "donation_id": donation_id})

    # Decide which separations fell short, now that every bag is assembled.
    yield_losses = apply_yield_losses(rng, productions)

    if yield_losses:
        print(
            "  separation losses:  "
            + ", ".join(f"{count:,} {code}" for code, count in yield_losses.items())
        )

    shortfall = wanted - len(reactive_indices)

    if shortfall > 0:
        print(
            f"  NOTE: {shortfall:,} reactive results could not be placed — only "
            f"{len(eligible):,} of {len(pending):,} donations have units that "
            "never left the shelf. Reactive rate is under target rather than "
            "implying an infected unit was transfused."
        )

    # Screening deferrals: donors turned away, who therefore have a screening
    # record and no donation. Generated on top rather than carved out, because
    # every unit that exists must still have a donation behind it.
    deferral_count = int(len(donations) * DEFERRAL_SHARE / (1 - DEFERRAL_SHARE))

    # The ABO/Rh mix of people turning up to donate, taken from the donations
    # actually recorded rather than assumed. Deriving it means it stays correct
    # if the demand model's blood group distribution ever changes.
    group_counts: dict[int, int] = defaultdict(int)

    for record in pending:
        group_counts[record["donation"]["typed_blood_group_id"]] += 1

    group_ids = sorted(group_counts) or sorted(group_code)
    totals = np.array(
        [group_counts.get(gid, 1) for gid in group_ids], dtype=float
    )
    group_probabilities = totals / totals.sum()

    rules = deferral_policy()
    reasons, weights = deferral_distribution(rules)

    session_ids_by_facility: dict[str, list[dict]] = defaultdict(list)

    for row in sessions:
        session_ids_by_facility[row["facility_id"]].append(row)

    facility_ids = [f for f in session_ids_by_facility if session_ids_by_facility[f]]

    for _ in range(deferral_count):
        facility_id = facility_ids[int(rng.integers(0, len(facility_ids)))]
        facility = facilities[facility_id]
        candidates = session_ids_by_facility[facility_id]
        chosen = candidates[int(rng.integers(0, len(candidates)))]
        day = chosen["scheduled_date"]

        # The group follows the population's ABO/Rh distribution. Drawing it
        # uniformly made AB-negative ten times commoner among deferred donors
        # than among donors generally, which skews every rare-group figure the
        # register reports.
        group_id = int(rng.choice(group_ids, p=group_probabilities))

        # Always a fresh registration, never a reuse. Reusing an existing donor
        # here could record someone as deferred on a day they are also recorded
        # as having donated, and the register would then contradict itself.
        donor_id = pool.mint(group_id, facility, day)

        gender = pool.gender.get(donor_id, "MALE")

        # The reason is drawn AFTER the donor, because some rules only apply to
        # one sex and drawing first produced male donors deferred for pregnancy.
        available, available_weights = applicable_reasons(
            rules, reasons, weights, gender
        )
        reason = available[int(rng.choice(len(available), p=available_weights))]
        rule = rules[reason]
        kind = str(rule.get("kind", "TIMED")).upper()

        # Only a TIMED deferral has an end date. A CONDITIONAL one lifts when the
        # finding resolves and a PERMANENT one never lifts, so neither may be
        # given a duration — writing one would let an eligibility check score the
        # donor available on a date that means nothing.
        defer_days = deferral_days_for(rule, gender) if kind == "TIMED" else 0
        screened_at = datetime.combine(
            day, datetime.min.time(), tzinfo=timezone.utc
        ) + timedelta(hours=float(rng.uniform(4, 14)))

        low_hb = reason == "LOW_HAEMOGLOBIN"
        haemoglobin = (
            # Below the bar, which is why they were turned away.
            float(np.clip(rng.normal(11.4, 0.8), 6.5, HAEMOGLOBIN_MIN - 0.1))
            if low_hb
            # Deferred for some other reason, so haemoglobin was never the issue.
            else float(np.clip(rng.normal(14.0, 1.2), HAEMOGLOBIN_MIN, 18.5))
        )

        screenings.append(
            {
                "id": str(uuid.uuid4()),
                "donor_id": donor_id,
                "session_id": chosen["id"],
                "facility_id": facility_id,
                "screened_at": screened_at,
                "haemoglobin_g_dl": round(haemoglobin, 1),
                # A donor deferred for being underweight must actually be below
                # the floor, or the screening record contradicts its own reason.
                "weight_kg": round(
                    float(
                        np.clip(rng.normal(42, 2.5), 34, WEIGHT_MIN_REDUCED - 0.1)
                        if reason == "UNDERWEIGHT"
                        else np.clip(rng.normal(66, 8), WEIGHT_MIN_REDUCED, 115)
                    ),
                    1,
                ),
                "systolic_bp": int(
                    np.clip(rng.normal(152 if reason == "HIGH_BLOOD_PRESSURE" else 121, 9), 95, 190)
                ),
                "diastolic_bp": int(np.clip(rng.normal(79, 9), 58, 118)),
                "pulse_bpm": int(np.clip(rng.normal(78, 10), 50, 118)),
                "temperature_c": round(
                    float(np.clip(rng.normal(38.4 if reason == "FEVER_OR_INFECTION" else 36.7, 0.3), 35.8, 39.5)), 1
                ),
                "questionnaire_json": {"flagged": reason},
                "outcome": "DEFERRED",
                "deferral_reason_code": reason,
                "deferral_days": defer_days or None,
                "screened_by": str(rng.choice(staff[facility_id]["collect"] or ["Duty Phlebotomist"])),
            }
        )
        deferred_screenings += 1

        # A deferral that never reaches the donor record is a deferral that
        # never happened: the register would keep offering this person to the
        # recall desk. 9,988 screening deferrals previously wrote nothing here.
        screened_by = screenings[-1]["screened_by"]

        if kind == "PERMANENT":
            pool.barred.add(donor_id)
            deferrals.append(
                {
                    "id": str(uuid.uuid4()),
                    "donor_id": donor_id,
                    "deferred_at": screened_at,
                    "deferred_until": None,
                    "is_permanent": True,
                    "reason_code": reason,
                    "reason_note": (
                        "Permanent deferral on screening. This does not expire "
                        "and is not re-assessed."
                    ),
                    "recorded_by": screened_by,
                }
            )
            donor_updates[donor_id] = {
                "d_id": donor_id,
                "is_permanently_deferred": True,
                "deferred_until": None,
                "availability_status": "PERMANENTLY_DEFERRED",
            }
        elif defer_days:
            until = (screened_at + timedelta(days=defer_days)).date()
            pool.barred.add(donor_id)
            deferrals.append(
                {
                    "id": str(uuid.uuid4()),
                    "donor_id": donor_id,
                    "deferred_at": screened_at,
                    "deferred_until": until,
                    "is_permanent": False,
                    "reason_code": reason,
                    "recorded_by": screened_by,
                }
            )
            donor_updates[donor_id] = {
                "d_id": donor_id,
                "is_permanently_deferred": False,
                "deferred_until": until,
                "availability_status": "TEMPORARILY_DEFERRED",
            }
        else:
            # defer_days of 0 means "not today" — underweight, for instance,
            # resolves whenever the donor gains the weight, not on a date. That
            # is a CONDITIONAL deferral: no end date, and it must not be written
            # as a permanent one either.
            pool.barred.add(donor_id)
            deferrals.append(
                {
                    "id": str(uuid.uuid4()),
                    "donor_id": donor_id,
                    "deferred_at": screened_at,
                    "deferred_until": None,
                    "is_permanent": False,
                    "reason_code": reason,
                    "reason_note": (
                        "Conditional deferral: no automatic end date. The donor "
                        "becomes eligible when the finding resolves, which must "
                        "be re-assessed at the chair."
                    ),
                    "recorded_by": screened_by,
                }
            )
            donor_updates[donor_id] = {
                "d_id": donor_id,
                "is_permanently_deferred": False,
                "deferred_until": None,
                "availability_status": "CONDITIONALLY_DEFERRED",
            }

    return {
        "sessions": sessions,
        "screenings": screenings,
        "donations": donations,
        "tests": tests,
        "productions": productions,
        "unit_links": unit_links,
        "discard_updates": discard_updates,
        "quarantine_updates": quarantine_updates,
        "deferrals": deferrals,
        "donor_updates": list(donor_updates.values()),
        "new_donors": pool.new_donors,
        "reactive": reactive_donations,
        "awaiting_results": awaiting_results,
        "deferred": deferred_screenings,
        "cutoff": cutoff,
    }


def clear_previous(session) -> None:
    """Remove everything a previous run of this module wrote.

    This runs BEFORE the history is built, not as part of writing it. The donor
    pool reads the register to decide who can donate again, so it must not see
    donors that are about to be deleted — otherwise it hands out identifiers
    that no longer exist by the time the screenings are inserted.
    """

    print("Clearing anything a previous run left behind.")

    # Unlink the units FIRST. blood_unit.donation_id is a foreign key into
    # donation, so deleting donations while the links stand fails the constraint.
    session.execute(BloodUnit.__table__.update().values(donation_id=None))

    session.execute(delete(DonationTest))
    session.execute(delete(ComponentProduction))
    session.execute(delete(Donation))
    session.execute(delete(DonorScreening))
    session.execute(delete(DonationSession))
    # Undo the donor status changes the previous run's deferrals caused, before
    # dropping the rows that record them. Only donors carrying a deferral row
    # from THIS module are touched — the seed register's own deferrals, set by
    # datagen.donors, have no such row and must survive.
    session.connection().exec_driver_sql(
        """
        UPDATE donor SET
            is_permanently_deferred = 0,
            deferred_until = NULL,
            availability_status = 'AVAILABLE'
        WHERE EXISTS (SELECT 1 FROM donor_deferral f WHERE f.donor_id = donor.id)
        """
    )
    session.execute(delete(DonorDeferral))

    # Donors last — screenings and donations point at them. Without this a
    # second run leaves the first run's pool orphaned: they keep a donation
    # count that no longer has any donations behind it, and the register
    # silently doubles in size.
    removed = session.execute(
        delete(Donor).where(Donor.donor_code.like(f"{GENERATED_DONOR_PREFIX}%"))
    ).rowcount

    if removed:
        print(f"    removed {removed:,} donors from the previous run")

    # Undo the previous run's reactive discards, otherwise re-running would
    # ratchet the discard count up every time. Only units that were AVAILABLE or
    # EXPIRED can carry this reason, so the original status is recoverable from
    # the expiry date alone.
    session.connection().exec_driver_sql(
        """
        UPDATE blood_unit SET
            status = CASE WHEN expires_at <= :now THEN 'EXPIRED' ELSE 'AVAILABLE' END,
            screening_status = 'PASSED',
            -- An expired unit is still a discarded unit, and it must keep a
            -- reason. Clearing these outright left 2,261 units EXPIRED with no
            -- discarded_at and no reason, which hides a fifth of expiry wastage
            -- from any reason-keyed query.
            discard_reason = CASE WHEN expires_at <= :now THEN 'EXPIRY' END,
            discarded_at = CASE WHEN expires_at <= :now THEN expires_at END
        WHERE discard_reason LIKE 'TTI_REACTIVE%'
        """,
        {"now": DEMO_DATETIME},
    )
    session.flush()

    session.commit()


def _write(session, built: dict) -> None:
    """Bulk insert the built history. `clear_previous` must have run first."""

    chunks = [
        ("donors", Donor, built["new_donors"]),
        ("sessions", DonationSession, built["sessions"]),
        ("screenings", DonorScreening, built["screenings"]),
        ("donations", Donation, built["donations"]),
        ("tests", DonationTest, built["tests"]),
        ("production", ComponentProduction, built["productions"]),
        ("deferrals", DonorDeferral, built["deferrals"]),
    ]

    for label, model, rows in chunks:
        print(f"  writing {len(rows):,} {label}...")

        for start in range(0, len(rows), 10000):
            session.bulk_insert_mappings(model, rows[start : start + 10000])

        session.flush()

    print(f"  linking {len(built['unit_links']):,} units to donations...")

    for start in range(0, len(built["unit_links"]), 10000):
        session.connection().execute(
            BloodUnit.__table__.update()
            .where(BloodUnit.id == bindparam("b_id"))
            .values(donation_id=bindparam("donation_id")),
            built["unit_links"][start : start + 10000],
        )

    quarantined = built.get("quarantine_updates") or []

    if quarantined:
        print(f"  quarantining {len(quarantined):,} units awaiting lab results...")

        for start in range(0, len(quarantined), 10000):
            session.connection().execute(
                BloodUnit.__table__.update()
                .where(BloodUnit.id == bindparam("b_id"))
                .values(
                    status=bindparam("status"),
                    screening_status=bindparam("screening_status"),
                ),
                quarantined[start : start + 10000],
            )

    discards = built["discard_updates"]

    if discards:
        print(f"  discarding {len(discards):,} units on reactive results...")

        for start in range(0, len(discards), 10000):
            session.connection().execute(
                BloodUnit.__table__.update()
                .where(BloodUnit.id == bindparam("b_id"))
                .values(
                    status=bindparam("status"),
                    screening_status=bindparam("screening_status"),
                    discard_reason=bindparam("discard_reason"),
                    discarded_at=bindparam("discarded_at"),
                ),
                discards[start : start + 10000],
            )

    updates = built["donor_updates"]

    if updates:
        print(f"  updating {len(updates):,} donor records from deferrals...")

        for start in range(0, len(updates), 10000):
            session.connection().execute(
                Donor.__table__.update()
                .where(Donor.id == bindparam("d_id"))
                .values(
                    is_permanently_deferred=bindparam("is_permanently_deferred"),
                    deferred_until=bindparam("deferred_until"),
                    availability_status=bindparam("availability_status"),
                ),
                updates[start : start + 10000],
            )

    session.commit()


def _refresh_donor_history(session) -> None:
    """Recompute each donor's donation counts from the records just written.

    Derived columns that disagree with the rows they summarise are worse than no
    columns at all, so they are rebuilt rather than incremented.
    """

    print("  recomputing donor donation history...")

    # availability_status is a denormalised convenience column, and a
    # denormalised column that drifts from its source is worse than no column:
    # it looks authoritative while being wrong. Recompute it from the deferral
    # ledger and the donation record so the two can never disagree.
    #
    # Order matters — the CASE arms are evaluated top down, so the most
    # restrictive state wins.
    session.connection().exec_driver_sql(
        """
        UPDATE donor SET availability_status = CASE
            WHEN is_permanently_deferred = 1 THEN 'PERMANENTLY_DEFERRED'
            WHEN EXISTS (SELECT 1 FROM donor_deferral f
                         WHERE f.donor_id = donor.id AND f.lifted_at IS NULL
                           AND f.is_permanent = 1)
                THEN 'PERMANENTLY_DEFERRED'
            WHEN EXISTS (SELECT 1 FROM donor_deferral f
                         WHERE f.donor_id = donor.id AND f.lifted_at IS NULL
                           AND f.deferred_until IS NULL
                           AND f.reason_code LIKE 'TTI_AWAITING%')
                THEN 'AWAITING_TTI_CONFIRMATION'
            WHEN EXISTS (SELECT 1 FROM donor_deferral f
                         WHERE f.donor_id = donor.id AND f.lifted_at IS NULL
                           AND f.deferred_until IS NULL)
                THEN 'CONDITIONALLY_DEFERRED'
            WHEN EXISTS (SELECT 1 FROM donor_deferral f
                         WHERE f.donor_id = donor.id AND f.lifted_at IS NULL
                           AND f.deferred_until > :today)
                THEN 'TEMPORARILY_DEFERRED'
            WHEN deferred_until IS NOT NULL AND deferred_until > :today
                THEN 'TEMPORARILY_DEFERRED'
            ELSE 'AVAILABLE'
        END
        """,
        {"today": DEMO_DATETIME.date()},
    )

    # A donor whose timed deferral has elapsed is eligible again, and the column
    # that says otherwise must be cleared with it.
    session.connection().exec_driver_sql(
        """
        UPDATE donor SET deferred_until = NULL
        WHERE deferred_until IS NOT NULL AND deferred_until <= :today
        """,
        {"today": DEMO_DATETIME.date()},
    )
    session.commit()


    session.connection().exec_driver_sql(
        """
        UPDATE donor SET
            total_donations = COALESCE((
                SELECT COUNT(*) FROM donation d WHERE d.donor_id = donor.id
            ), 0),
            first_donation_at = (
                SELECT MIN(d.collected_at) FROM donation d WHERE d.donor_id = donor.id
            ),
            last_donation_at = (
                SELECT MAX(d.collected_at) FROM donation d WHERE d.donor_id = donor.id
            )
        WHERE EXISTS (SELECT 1 FROM donation d WHERE d.donor_id = donor.id)
        """
    )

    # A donor created by this module with no donation behind them has no legacy
    # history to inherit, so their counters must read zero. Seed donors are left
    # alone: their totals are a migrated opening balance from the register that
    # existed before go-live, and those donations genuinely have no rows here.
    session.connection().exec_driver_sql(
        f"""
        UPDATE donor SET
            total_donations = 0,
            first_donation_at = NULL,
            last_donation_at = NULL
        WHERE donor_code LIKE '{GENERATED_DONOR_PREFIX}%'
          AND NOT EXISTS (SELECT 1 FROM donation d WHERE d.donor_id = donor.id)
        """
    )
    session.commit()


def main() -> None:
    default_window = int(config.get("synthetic.operational_window_days", 45))
    parser = argparse.ArgumentParser(
        description="Retro-fit donations, screenings and tests onto existing units."
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=default_window,
        help=f"How far back the operational history goes (default {default_window}).",
    )
    args = parser.parse_args()

    init_db()
    session = SessionLocal()
    rng = np.random.default_rng(SEED)

    try:
        clear_previous(session)

        print(f"Building operational history for the last {args.window_days} days.")
        built = build(session, args.window_days, rng)

        print()
        print(f"  new donors needed:  {len(built['new_donors']):,}")
        print(f"  donations:          {len(built['donations']):,}")
        print(f"  reactive:           {built['reactive']:,}")
        print(f"  awaiting results:   {built['awaiting_results']:,}"
              f"  (awaiting the next plate)")

        permanent = sum(1 for d in built["deferrals"] if d["is_permanent"])
        timed = sum(
            1 for d in built["deferrals"]
            if not d["is_permanent"] and d["deferred_until"]
        )
        conditional = len(built["deferrals"]) - permanent - timed

        print(f"  donor deferrals:    {len(built['deferrals']):,}"
              f"  ({permanent:,} permanent, {timed:,} timed, "
              f"{conditional:,} conditional)")
        print(f"  screenings:         {len(built['screenings']):,}"
              f" ({built['deferred']:,} deferred)")
        print(f"  TTI test results:   {len(built['tests']):,}")
        print()

        _write(session, built)
        _refresh_donor_history(session)

        print()
        print("Done.")

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


# ------------------------------------------------------------- yield loss
#
# `units_expected == units_produced` on every separation asserts a processing
# loss rate of zero, which no blood bank has, and makes the yield report
# unreadable because it can only print 100%.
#
# The fix uses the data as it is. A bag that produced red cells and plasma is
# indistinguishable from one that was meant to produce a platelet too and lost
# it — same units, same record. So a share of those are written as
# three-component separations that fell short, at the rate config specifies,
# with the cause attributed.

LOSS_RATES = dict(config.get("processing.yield_loss_rate") or {})
LOSS_REASONS_BY_COMPONENT = dict(config.get("processing.loss_reasons") or {})
PROCESSING_RECIPES = dict(config.get("processing.recipes") or {})


def separation_record(
    rng,
    *,
    donation_id: str,
    facility_id: str,
    collected_at,
    produced_codes: list[str],
    bag_type: str,
    produced_by: str,
    tested_at,
) -> dict:
    """One separation, with an honest expected-versus-produced.

    `produced_codes` is what the bag actually yielded. Whether it was EXPECTED to
    yield more is decided here, because that intent was never recorded anywhere
    else and a two-unit bag carries no evidence either way.
    """

    produced = sorted(produced_codes)

    # Expected starts equal to produced. Which of these bags was MEANT to yield
    # more is decided in one pass afterwards, by `apply_yield_losses`, because
    # the loss rate is defined against bags that intended the component and that
    # denominator is not known until every bag has been assembled.
    expected = list(produced)
    losses: dict[str, str] = {}

    minutes = int((tested_at - collected_at).total_seconds() / 60)

    return {
        "id": str(uuid.uuid4()),
        "donation_id": donation_id,
        "facility_id": facility_id,
        "produced_at": tested_at,
        "method": (
            "APHERESIS"
            if bag_type == "APHERESIS_KIT"
            else "BUFFY_COAT"
            if len(produced) >= 3
            else "CENTRIFUGATION"
        ),
        "recipe_code": bag_type,
        "units_expected": len(expected),
        "units_produced": len(produced),
        "expected_components": expected,
        "produced_components": produced,
        "loss_reasons": losses or None,
        "minutes_from_collection": max(0, minutes),
        "produced_by": produced_by,
    }


def apply_yield_losses(rng, productions: list[dict]) -> dict[str, int]:
    """Mark exactly the configured share of separations as having fallen short.

    A bag that produced red cells and plasma is indistinguishable from one that
    was meant to produce a platelet as well and lost it, so a share of those are
    rewritten as three-component separations that fell short.

    Assigned as a pass over the finished list rather than per-bag, because the
    rate is defined against bags that INTENDED the component and that
    denominator is only known once every bag has been assembled. Applying a
    per-bag probability meant guessing it, and the guess produced 1.84% against
    a configured 5.5%.
    """

    applied: dict[str, int] = {}

    for component, downgrade_from in (("PLT_RD", 3), ("FFP", 2)):
        rate = float(LOSS_RATES.get(component, 0.0))

        if rate <= 0:
            continue

        # Bags that already yield the component: these are the successes.
        successes = [
            row
            for row in productions
            if component in (row["produced_components"] or [])
        ]

        # Bags whose shape is consistent with having lost it: one component
        # short, and missing exactly this one.
        candidates = [
            row
            for row in productions
            if len(row["produced_components"] or []) == downgrade_from - 1
            and component not in (row["produced_components"] or [])
            and "PRBC" in (row["produced_components"] or [])
            and not row.get("loss_reasons")
        ]

        if not candidates:
            continue

        # losses / (successes + losses) == rate  ->  losses = rate*successes/(1-rate)
        wanted = int(round(rate * len(successes) / max(1e-9, 1.0 - rate)))
        take = min(wanted, len(candidates))

        if take <= 0:
            continue

        reasons = LOSS_REASONS_BY_COMPONENT.get(component) or ["BAG_DAMAGED"]
        chosen = rng.choice(len(candidates), size=take, replace=False)

        for index in np.atleast_1d(chosen):
            row = candidates[int(index)]
            expected = sorted(set(row["produced_components"]) | {component})

            row["expected_components"] = expected
            row["units_expected"] = len(expected)
            row["loss_reasons"] = {component: str(rng.choice(reasons))}

        applied[component] = take

    return applied


if __name__ == "__main__":
    main()
