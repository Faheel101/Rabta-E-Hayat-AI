"""Seed organizations and user accounts, and assign facilities to tenants.

    python -m scripts.seed_tenants

The 30 seeded facilities are distributed across organizations that mirror how
Pakistani blood banking is actually structured: a government teaching-hospital
group, a couple of private hospital groups, specialist standalones, and the two
Regional Blood Centre operators that run the hub-and-spoke networks.

Sharing consent is set per facility, not per organization, and deliberately not
universal — the network layer has to be demonstrably a choice.
"""

from __future__ import annotations

from sqlalchemy import select

from db.models import Facility, Organization, UserAccount, UserSession, new_id
from db.session import SessionLocal, init_db
from web.security import hash_password

DEMO_PASSWORD = "Rabta@2026"

# code, name, type, facility codes it owns, network opt-in
ORGANIZATIONS = [
    (
        "PUNJAB_TEACHING",
        "Punjab Teaching Hospitals Group",
        "HOSPITAL_GROUP",
        [
            "SERVICES_LAHORE",
            "MAYO_LAHORE",
            "JINNAH_LAHORE",
            "GENERAL_LAHORE",
            "SHEIKH_ZAYED_LHR",
        ],
        True,
    ),
    (
        "RBC_PUNJAB_NORTH",
        "Regional Blood Centre Lahore",
        "RBC_OPERATOR",
        ["RBC_LAHORE", "DHQ_KASUR", "DHQ_SHEIKHUPURA", "THQ_FEROZEWALA"],
        True,
    ),
    (
        "RBC_PUNJAB_SOUTH",
        "Regional Blood Centre Multan",
        "RBC_OPERATOR",
        ["RBC_MULTAN", "DHQ_KHANEWAL", "DHQ_VEHARI", "DHQ_SAHIVAL"],
        True,
    ),
    (
        "CHILDREN_TRUST",
        "Children's Hospital & Institute of Child Health",
        "STANDALONE_HOSPITAL",
        ["CHILDREN_LAHORE"],
        True,
    ),
    (
        "SHAUKAT_KHANUM",
        "Shaukat Khanum Memorial Trust",
        "STANDALONE_HOSPITAL",
        ["SHKAT_LAHORE"],
        # Deliberately not sharing: a private cancer centre with a tightly
        # managed platelet supply is exactly the case where opting out is
        # realistic, and it makes the consent boundary visible in the demo.
        False,
    ),
    (
        "GULAB_DEVI",
        "Gulab Devi Chest Hospital",
        "STANDALONE_HOSPITAL",
        ["GULAB_LAHORE"],
        True,
    ),
    (
        "RAWALPINDI_MED",
        "Rawalpindi Medical University Hospitals",
        "HOSPITAL_GROUP",
        ["BBH_RAWALPINDI", "HOLY_RAWALPINDI", "DHQ_JHELUM"],
        True,
    ),
    (
        "FAISALABAD_MED",
        "Faisalabad Medical University Hospitals",
        "HOSPITAL_GROUP",
        ["ALLIED_FAISALABAD", "DHQ_FAISALABAD", "DHQ_JHANG"],
        True,
    ),
    (
        "NISHTAR",
        "Nishtar Medical University Hospital",
        "STANDALONE_HOSPITAL",
        ["NISHTAR_MULTAN"],
        True,
    ),
    (
        "SOUTH_PUNJAB_DHQ",
        "South Punjab District Health Authority",
        "GOVT_PROGRAMME",
        [
            "BVH_BAHAWALPUR",
            "DHQ_RAHIMYARHAN",
            "DHQ_DGKHAN",
            "DHQ_GUJRANWALA",
            "DHQ_SIALKOT",
            "DHQ_SARGODHA",
            "DHQ_BHAKKAR",
        ],
        True,
    ),
]

# email local part, full name, job title, role, organization code, facility code
# (None = group-level user working across the organization's facilities)
USERS = [
    (
        "dr.ahmed",
        "Dr. Ahmed Raza",
        "Blood Bank Officer",
        "BLOOD_BANK_OFFICER",
        "PUNJAB_TEACHING",
        "JINNAH_LAHORE",
    ),
    (
        "s.fatima",
        "Sadia Fatima",
        "Group Transfusion Coordinator",
        "RBC_COORDINATOR",
        "PUNJAB_TEACHING",
        None,
    ),
    (
        "dr.khan",
        "Dr. Bilal Khan",
        "RBC Coordinator",
        "RBC_COORDINATOR",
        "RBC_PUNJAB_NORTH",
        "RBC_LAHORE",
    ),
    (
        "m.iqbal",
        "Muhammad Iqbal",
        "Blood Bank Officer",
        "BLOOD_BANK_OFFICER",
        "RBC_PUNJAB_SOUTH",
        "RBC_MULTAN",
    ),
    (
        "dr.zainab",
        "Dr. Zainab Sheikh",
        "Consultant Haematologist",
        "BLOOD_BANK_OFFICER",
        "CHILDREN_TRUST",
        "CHILDREN_LAHORE",
    ),
    (
        "a.hussain",
        "Ayesha Hussain",
        "Blood Bank Manager",
        "BLOOD_BANK_OFFICER",
        "SHAUKAT_KHANUM",
        "SHKAT_LAHORE",
    ),
    (
        "dr.tariq",
        "Dr. Tariq Mahmood",
        "Provincial Administrator",
        "PROVINCIAL_ADMIN",
        "SOUTH_PUNJAB_DHQ",
        None,
    ),
    (
        "control.room",
        "Provincial Emergency Cell",
        "Emergency Controller",
        "EMERGENCY_CONTROLLER",
        "SOUTH_PUNJAB_DHQ",
        None,
    ),
    (
        "admin",
        "System Administrator",
        "System Administrator",
        "SYSTEM_ADMIN",
        "PUNJAB_TEACHING",
        None,
    ),
    # Bench staff. Two at Jinnah Lahore because the two-person release rule needs
    # two people who can actually sign — one lab technologist cannot verify their
    # own result, and a facility with a single tester cannot release at all.
    (
        "n.bibi",
        "Nasreen Bibi",
        "Senior Phlebotomist",
        "PHLEBOTOMIST",
        "PUNJAB_TEACHING",
        "JINNAH_LAHORE",
    ),
    (
        "r.aslam",
        "Rizwan Aslam",
        "Lab Technologist",
        "LAB_TECHNOLOGIST",
        "PUNJAB_TEACHING",
        "JINNAH_LAHORE",
    ),
    (
        "f.noor",
        "Farah Noor",
        "Lab Technologist",
        "LAB_TECHNOLOGIST",
        "PUNJAB_TEACHING",
        "JINNAH_LAHORE",
    ),
    (
        "k.shah",
        "Kamran Shah",
        "Phlebotomist",
        "PHLEBOTOMIST",
        "RBC_PUNJAB_NORTH",
        "RBC_LAHORE",
    ),
    (
        "s.malik",
        "Saima Malik",
        "Lab Technologist",
        "LAB_TECHNOLOGIST",
        "RBC_PUNJAB_NORTH",
        "RBC_LAHORE",
    ),
    (
        "h.javed",
        "Hassan Javed",
        "Lab Technologist",
        "LAB_TECHNOLOGIST",
        "RBC_PUNJAB_NORTH",
        "RBC_LAHORE",
    ),
]

EMAIL_DOMAIN = "rabta.pk"

# Facilities that publish stock to the network. Teaching hospitals and RBCs share;
# a private cancer centre does not.
NON_SHARING_FACILITIES = {"SHKAT_LAHORE"}


def main() -> None:
    init_db()
    session = SessionLocal()

    try:
        facilities = {
            facility.code: facility
            for facility in session.scalars(select(Facility)).all()
        }

        if not facilities:
            raise RuntimeError(
                "No facilities found. Run scripts.seed_reference first."
            )

        print("Seeding organizations...")
        org_by_code: dict[str, Organization] = {}

        for code, name, org_type, facility_codes, opt_in in ORGANIZATIONS:
            org = session.scalar(
                select(Organization).where(Organization.code == code)
            )

            if org is None:
                org = Organization(
                    id=new_id(),
                    code=code,
                    name_en=name,
                    org_type=org_type,
                    province="Punjab",
                    contact_email=f"bloodbank@{code.lower()}.{EMAIL_DOMAIN}",
                    network_opt_in=opt_in,
                    is_active=True,
                )
                session.add(org)
                session.flush()
            else:
                org.name_en = name
                org.org_type = org_type
                org.network_opt_in = opt_in

            org_by_code[code] = org

            for facility_code in facility_codes:
                facility = facilities.get(facility_code)

                if facility is None:
                    print(f"  ! unknown facility {facility_code}, skipped")
                    continue

                facility.organization_id = org.id
                facility.shares_inventory = (
                    opt_in and facility_code not in NON_SHARING_FACILITIES
                )
                facility.shares_contact = facility.shares_inventory
                facility.network_response_sla_minutes = (
                    60 if facility.facility_type == "RBC" else 120
                )

        session.flush()

        orphans = [
            facility.code
            for facility in facilities.values()
            if facility.organization_id is None
        ]

        if orphans:
            print(f"  ! {len(orphans)} facilities have no organization: {orphans}")

        print("Seeding user accounts...")
        password_hash = hash_password(DEMO_PASSWORD)
        created = 0

        for local, full_name, title, role, org_code, facility_code in USERS:
            org = org_by_code.get(org_code)

            if org is None:
                continue

            email = f"{local}@{org_code.lower().replace('_', '-')}.{EMAIL_DOMAIN}"

            user = session.scalar(
                select(UserAccount).where(UserAccount.email == email)
            )

            facility_id = (
                facilities[facility_code].id
                if facility_code and facility_code in facilities
                else None
            )

            if user is None:
                session.add(
                    UserAccount(
                        id=new_id(),
                        organization_id=org.id,
                        facility_id=facility_id,
                        email=email,
                        password_hash=password_hash,
                        full_name=full_name,
                        job_title=title,
                        role=role,
                        is_active=True,
                    )
                )
                created += 1
            else:
                user.organization_id = org.id
                user.facility_id = facility_id
                user.full_name = full_name
                user.job_title = title
                user.role = role
                user.password_hash = password_hash
                user.is_active = True
                user.failed_login_count = 0
                user.locked_until = None

        # Any session issued before a re-seed refers to a state that no longer
        # holds; ending them is safer than leaving them half-valid.
        session.query(UserSession).delete(synchronize_session=False)

        session.commit()

        print()
        print(f"Organizations: {len(org_by_code)}")
        print(f"Users:         {len(USERS)} ({created} new)")
        print(f"Password:      {DEMO_PASSWORD}")
        print()
        print("Accounts:")

        rows = session.execute(
            select(
                UserAccount.email,
                UserAccount.role,
                Organization.name_en,
                Facility.name_en,
            )
            .join(Organization, Organization.id == UserAccount.organization_id)
            .outerjoin(Facility, Facility.id == UserAccount.facility_id)
            .order_by(Organization.name_en, UserAccount.email)
        ).all()

        for email, role, org_name, facility_name in rows:
            where = facility_name or "group-level (all facilities)"
            print(f"  {email:44s} {role:22s} {org_name} — {where}")

        shared = session.scalar(
            select(Facility)
            .where(Facility.shares_inventory.is_(True))
            .limit(1)
        )
        share_count = len(
            session.scalars(
                select(Facility.id).where(Facility.shares_inventory.is_(True))
            ).all()
        )

        print()
        print(
            f"Network sharing: {share_count} of {len(facilities)} facilities publish "
            "stock; the rest are opted out."
        )

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


if __name__ == "__main__":
    main()
