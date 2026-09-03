"""Donor identity and donation history (spec §15).

The 5,000 donors generated earlier carry only a code, a group and a district,
because at the time the system was a decision layer that deliberately held no
donor identity (spec §13.2). Building the blood bank itself means the donor
register is a real screen that staff search by name and phone, so the population
needs plausible identity.

Everything here is synthetic and seeded. CNICs are generated, then stored as a
salted hash with only the last four digits in clear — the same treatment real
entries get, so the demo exercises the production path rather than a shortcut.

Donor mix reflects Pakistani practice: the large majority of collections are
replacement (family) donations against a specific patient, not voluntary
non-remunerated donation. A system that assumed otherwise would mis-model the
dominant workflow.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date, timedelta

import numpy as np
from sqlalchemy import select

from core import config, eligibility
from core.clock import DEMO_DATETIME, as_utc
from db.models import BloodGroup, Donor, Facility, Organization
from db.session import SessionLocal, init_db

SEED = config.SEED

# A deliberately small, plausible pool. Real registers have far more variety, but
# a demo reads better when names are recognisable than when they are random
# syllables.
MALE_GIVEN = [
    "Muhammad", "Ahmed", "Ali", "Hassan", "Usman", "Bilal", "Faisal", "Imran",
    "Kamran", "Zeeshan", "Adnan", "Tariq", "Asif", "Nadeem", "Waqar", "Shahid",
    "Rizwan", "Saad", "Umair", "Hamza", "Junaid", "Naveed", "Salman", "Yasir",
    "Abdullah", "Danish", "Farhan", "Haris", "Irfan", "Khalid", "Ahsan", "Amir",
    "Arslan", "Asad", "Ayaz", "Azhar", "Babar", "Basit", "Ehsan", "Fahad",
    "Fawad", "Ghulam", "Habib", "Haider", "Ibrahim", "Ijaz", "Jawad", "Kashif",
    "Mansoor", "Mubashir", "Mudassar", "Munir", "Noman", "Owais", "Qasim",
    "Rehan", "Sajid", "Shakeel", "Sohail", "Talha", "Wajid", "Zahid", "Zubair",
    "Aftab", "Akbar", "Anwar", "Aqib", "Bashir", "Furqan", "Gulzar", "Hafeez",
    "Iftikhar", "Jamshed", "Liaqat", "Mazhar", "Nasir", "Pervaiz", "Raheel",
    "Sarfraz", "Shafiq", "Tanveer", "Ubaid", "Waseem", "Yousaf", "Zafar",
]

FEMALE_GIVEN = [
    "Ayesha", "Fatima", "Zainab", "Sadia", "Hina", "Maria", "Sana", "Nida",
    "Rabia", "Saima", "Farah", "Amna", "Kiran", "Mehwish", "Nazia", "Samina",
    "Sumbal", "Uzma", "Iqra", "Javeria", "Komal", "Laiba", "Maham", "Noor",
    "Aiman", "Anum", "Areeba", "Asma", "Bushra", "Erum", "Faiza", "Ghazala",
    "Hafsa", "Humaira", "Ifra", "Kanwal", "Khadija", "Mahnoor", "Mariam",
    "Nadia", "Naila", "Nimra", "Nusrat", "Rimsha", "Rubina", "Saba", "Shaista",
    "Shazia", "Sidra", "Sobia", "Tahira", "Tayyaba", "Yasmin", "Zoya", "Naseem",
    "Parveen", "Rukhsana", "Shabana", "Sumaira", "Zarina",
]

SURNAMES = [
    "Khan", "Ahmed", "Ali", "Hussain", "Malik", "Butt", "Chaudhry", "Sheikh",
    "Qureshi", "Raza", "Iqbal", "Javed", "Mahmood", "Nawaz", "Rashid", "Saleem",
    "Siddiqui", "Zafar", "Abbasi", "Bhatti", "Dar", "Gill", "Hashmi", "Jamil",
    "Kiani", "Lodhi", "Mirza", "Niazi", "Rana", "Warraich", "Ansari", "Arain",
    "Awan", "Baig", "Bajwa", "Bhutta", "Cheema", "Dogar", "Farooqi", "Ghauri",
    "Gondal", "Hanif", "Jatoi", "Kamboh", "Kazmi", "Khokhar", "Latif", "Marwat",
    "Mughal", "Naqvi", "Pasha", "Rajput", "Ranjha", "Sandhu", "Sial", "Soomro",
    "Sultan", "Tarar", "Virk", "Yousafzai", "Zaidi", "Alvi", "Durrani",
    "Janjua", "Sipra", "Wattoo",
]


def make_name(rng: np.random.Generator, is_male: bool) -> str:
    """A Pakistani personal name with realistic structure.

    Three parts are common — a religious or honorific first element, a personal
    name, then a family or caste name — so the pools alone would collide far too
    often. With 78,000 donors in the register a two-part name from a 30x30 pool
    produced 98 people called "Muhammad Mahmood", which makes a search screen
    useless and makes the data look generated. Real registers disambiguate on
    CNIC precisely because names do repeat, but not at that rate.
    """

    given_pool = MALE_GIVEN if is_male else FEMALE_GIVEN
    given = str(rng.choice(given_pool))
    surname = str(rng.choice(SURNAMES))

    if is_male:
        # "Muhammad" as a prefixed first element is extremely common, but it is
        # a prefix rather than the personal name — so it takes a middle name.
        if given == "Muhammad":
            middle = str(rng.choice([n for n in MALE_GIVEN if n != "Muhammad"]))
            return f"Muhammad {middle} {surname}"

        if rng.random() < 0.34:
            return f"Muhammad {given} {surname}"

        if rng.random() < 0.22:
            middle = str(rng.choice(MALE_GIVEN))
            return f"{given} {middle} {surname}"

        return f"{given} {surname}"

    # Women's names take a second element less often, and "Bibi" or "Begum" as a
    # standalone family name remains common in rural Punjab.
    if rng.random() < 0.18:
        return f"{given} {rng.choice(['Bibi', 'Begum', 'Kausar', 'Akhtar'])}"

    if rng.random() < 0.20:
        middle = str(rng.choice(FEMALE_GIVEN))
        return f"{given} {middle} {surname}"

    return f"{given} {surname}"


# Pakistan collects predominantly through replacement donation. Voluntary
# non-remunerated donation is what national policy pushes towards, so the split
# here is the current reality rather than the target.
DONOR_TYPE_WEIGHTS = {
    "REPLACEMENT": 0.72,
    "VOLUNTARY": 0.24,
    "DIRECTED": 0.04,
}

AGE_BAND_RANGE = {
    "18-25": (18, 25),
    "26-35": (26, 35),
    "36-45": (36, 45),
    "46-55": (46, 55),
    "56-65": (56, 60),
}

# Men are heavily over-represented among Pakistani donors; female donation rates
# are low, largely because of anaemia prevalence and social factors. Modelling an
# even split would make the haemoglobin deferral pattern unrealistic.
MALE_SHARE = 0.88


def cnic_salt() -> bytes:
    """Salt for the CNIC hash.

    In production this belongs in a managed secret store (spec §13.2 puts secrets
    in KMS, never in the repo). For the synthetic network a configured value is
    enough, and the point is that the column never holds a clear CNIC.
    """

    return (os.getenv("CNIC_SALT") or "rabta-dev-salt").encode("utf-8")


def hash_cnic(cnic: str) -> str:
    digits = "".join(character for character in cnic if character.isdigit())

    return hashlib.sha256(cnic_salt() + digits.encode("utf-8")).hexdigest()


def make_cnic(rng: np.random.Generator) -> str:
    """A syntactically plausible CNIC: 5 digits, 7 digits, 1 check-ish digit.

    Deliberately not a real issued number; the format matters because staff type
    it and the field validates, the value does not.
    """

    area = int(rng.integers(11000, 82999))
    serial = int(rng.integers(1000000, 9999999))
    last = int(rng.integers(0, 10))

    return f"{area:05d}-{serial:07d}-{last}"


def make_phone(rng: np.random.Generator) -> str:
    network = int(rng.choice([300, 301, 302, 320, 321, 331, 333, 340, 345]))
    number = int(rng.integers(1000000, 9999999))

    return f"0{network}-{number:07d}"


def choose(rng: np.random.Generator, weights: dict) -> str:
    keys = list(weights)
    probabilities = np.array([weights[key] for key in keys], dtype=float)
    probabilities /= probabilities.sum()

    return str(rng.choice(keys, p=probabilities))


def backfill(session, rng: np.random.Generator) -> dict:
    facilities = session.scalars(select(Facility)).all()
    facilities_by_id = {facility.id: facility for facility in facilities}
    groups = {group.id: group for group in session.scalars(select(BloodGroup)).all()}

    by_district: dict[str, list[Facility]] = {}

    for facility in facilities:
        by_district.setdefault(facility.district, []).append(facility)

    fallback = [facility for facility in facilities if facility.facility_type == "RBC"]
    fallback = fallback or facilities

    # Only the seed register. Donors minted by `datagen.operations` already have
    # a complete identity and a donation history that this backfill would
    # overwrite — including deferral flags, which would then contradict the
    # donations those donors have on record.
    donors = session.scalars(
        select(Donor)
        .where(Donor.donor_code.not_like("D-%"))
        .order_by(Donor.donor_code)
    ).all()

    stats = {
        "total": len(donors),
        "named": 0,
        "deferred": 0,
        "permanently_deferred": 0,
        "by_type": {},
        "female": 0,
    }

    today = DEMO_DATETIME.date()

    for donor in donors:
        is_male = bool(rng.random() < MALE_SHARE)
        full_name = make_name(rng, is_male)

        cnic = make_cnic(rng)

        band = donor.age_band or "26-35"
        low, high = AGE_BAND_RANGE.get(band, (26, 35))
        age = int(rng.integers(low, high + 1))

        # A birthday somewhere in the year, so age filters exercise real dates.
        birth_year = today.year - age
        date_of_birth = date(birth_year, int(rng.integers(1, 13)), int(rng.integers(1, 29)))

        facility = facilities_by_id.get(donor.registered_facility_id)

        if facility is None:
            home = by_district.get(donor.district) or fallback
            facility = home[int(rng.integers(0, len(home)))]

        donor.full_name = full_name
        donor.gender = "MALE" if is_male else "FEMALE"
        donor.date_of_birth = date_of_birth
        donor.cnic_hash = hash_cnic(cnic)
        donor.cnic_last4 = cnic[-4:]
        donor.phone = make_phone(rng)
        donor.address = f"{donor.city}, {donor.district}"

        donor.registered_facility_id = facility.id
        donor.organization_id = facility.organization_id

        donor_type = choose(rng, DONOR_TYPE_WEIGHTS)
        donor.donor_type = donor_type

        # Donation history. A donor with a recorded last donation has given at
        # least once, and their group is confirmed because the lab typed it.
        last_donation_at = as_utc(donor.last_donation_at)

        if last_donation_at is not None:
            donations = int(rng.integers(1, 12))
            donor.total_donations = donations

            # A donor with n donations has been on the register for at least
            # (n-1) intervals. The upper bound must exceed the lower even when
            # n is 1, which is the single-donation case.
            spread_days = int(rng.integers(90, 90 * donations + 90))
            donor.first_donation_at = last_donation_at - timedelta(days=spread_days)
            donor.blood_group_confirmed = True
        else:
            donor.total_donations = 0
            donor.first_donation_at = None
            donor.blood_group_confirmed = False

        donor.consent_contact = bool(rng.random() < 0.86)

        # A small share are currently deferred. Permanent deferrals are rare and
        # come from a reactive screening result, which the lab module will create
        # for real; here they exist so the register's filters have something to
        # find on day one.
        donor.deferred_until = None
        donor.is_permanently_deferred = False

        draw = float(rng.random())

        if draw < 0.012:
            donor.is_permanently_deferred = True
            donor.availability_status = "PERMANENTLY_DEFERRED"
            stats["permanently_deferred"] += 1
        elif draw < 0.10:
            donor.deferred_until = today + timedelta(days=int(rng.integers(5, 180)))
            donor.availability_status = "DEFERRED"
            stats["deferred"] += 1
        elif last_donation_at is not None:
            # The inter-donation interval is a hard rule, so availability is
            # derived from it rather than stored independently. The interval
            # itself comes from configuration, not from a literal here, because
            # it is a clinical parameter that has to be signed off and may differ
            # between organisations.
            days_since = (DEMO_DATETIME - last_donation_at).days
            required = eligibility.interval_days(donor.gender or "MALE")
            donor.availability_status = (
                "AVAILABLE" if days_since >= required else "RECENTLY_DONATED"
            )
        else:
            donor.availability_status = "AVAILABLE"

        stats["named"] += 1
        stats["by_type"][donor_type] = stats["by_type"].get(donor_type, 0) + 1

        if not is_male:
            stats["female"] += 1

    return stats


def main() -> None:
    init_db()
    session = SessionLocal()

    try:
        rng = np.random.default_rng(SEED + 17)

        print("Backfilling donor identity and history...")
        stats = backfill(session, rng)

        session.commit()

        print()
        print(f"Donors:               {stats['total']:,}")
        print(f"  with identity:      {stats['named']:,}")
        print(f"  female:             {stats['female']:,} "
              f"({100.0 * stats['female'] / max(1, stats['total']):.1f}%)")
        print(f"  temporarily deferred {stats['deferred']:,}")
        print(f"  permanently deferred {stats['permanently_deferred']:,}")
        print()
        print("By donor type:")

        for donor_type, count in sorted(
            stats["by_type"].items(), key=lambda item: item[1], reverse=True
        ):
            share = 100.0 * count / max(1, stats["total"])
            print(f"  {donor_type:14s} {count:6,}  {share:5.1f}%")

        print()
        print("CNICs are stored hashed with only the last four digits in clear.")

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


if __name__ == "__main__":
    main()
