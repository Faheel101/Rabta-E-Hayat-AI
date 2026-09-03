from sqlalchemy import func, select

from core.policy import build_group_shares, expand_reserve_policy, facility_policy
from db.models import BloodGroup, Component, Compatibility, Facility
from db.session import SessionLocal, init_db


BLOOD_GROUPS = [
    ("B+", "B", "+", 33.0),
    ("O+", "O", "+", 30.0),
    ("A+", "A", "+", 22.0),
    ("AB+", "AB", "+", 8.0),
    ("O-", "O", "-", 2.5),
    ("B-", "B", "-", 2.0),
    ("A-", "A", "-", 1.7),
    ("AB-", "AB", "-", 0.8),
]

COMPONENTS = [
    # code, name_en, name_ur, shelf_life_days, temp_min, temp_max, requires_agitation, max_transport_hours, criticality_weight
    ("WB", "Whole Blood", None, 35, 2.0, 6.0, False, 24.0, 0.8),
    ("PRBC", "Packed Red Blood Cells", None, 35, 2.0, 6.0, False, 24.0, 0.9),
    ("PLT_RD", "Random Donor Platelets", None, 5, 20.0, 24.0, True, 6.0, 1.0),
    ("PLT_APH", "Apheresis Platelets", None, 5, 20.0, 24.0, True, 6.0, 1.0),
    ("FFP", "Fresh Frozen Plasma", None, 365, -30.0, -25.0, False, 24.0, 0.8),
    ("CRYO", "Cryoprecipitate", None, 365, -30.0, -25.0, False, 24.0, 0.7),
]

# PRBC only. Whole blood has its own matrix below because its plasma content
# makes it ABO-identical-or-nothing.
RBC_COMPONENTS = {"PRBC"}
WHOLE_BLOOD_COMPONENTS = {"WB"}
PLASMA_COMPONENTS = {"FFP", "CRYO"}
PLATELET_COMPONENTS = {"PLT_RD", "PLT_APH"}

# Hub-and-spoke linkage (spec §4.2). Punjab's divisions are split between the
# two Regional Blood Centres; this is what lets an RBC be stocked against its
# spokes' demand rather than its own.
DIVISION_TO_RBC = {
    "Lahore": "RBC_LAHORE",
    "Rawalpindi": "RBC_LAHORE",
    "Faisalabad": "RBC_LAHORE",
    "Gujranwala": "RBC_LAHORE",
    "Sargodha": "RBC_LAHORE",
    "Multan": "RBC_MULTAN",
    "Bahawalpur": "RBC_MULTAN",
    "D.G. Khan": "RBC_MULTAN",
    "Sahiwal": "RBC_MULTAN",
}

# Red cell compatibility. Recipient -> allowed donor groups.
#
# This matrix is for SEPARATED red cells only. It must not be applied to whole
# blood: a unit of whole blood carries the donor's plasma as well as their red
# cells, so group O whole blood given to a group A patient delivers anti-A
# antibodies straight into that patient's circulation. "O is the universal red
# cell donor" is true of packed cells and false of whole blood — the single most
# common way this distinction is got wrong.
RBC_MATRIX = {
    "O-": ["O-"],
    "O+": ["O-", "O+"],
    "A-": ["O-", "A-"],
    "A+": ["O-", "O+", "A-", "A+"],
    "B-": ["O-", "B-"],
    "B+": ["O-", "O+", "B-", "B+"],
    "AB-": ["O-", "A-", "B-", "AB-"],
    "AB+": ["O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"],
}

# Whole blood compatibility. ABO must be IDENTICAL, because the unit carries the
# donor's plasma along with their red cells. Rh is the one axis with any give:
# Rh-negative whole blood may go to an Rh-positive recipient, never the reverse
# outside an explicit emergency override.
WHOLE_BLOOD_MATRIX = {
    "O-": ["O-"],
    "O+": ["O-", "O+"],
    "A-": ["A-"],
    "A+": ["A-", "A+"],
    "B-": ["B-"],
    "B+": ["B-", "B+"],
    "AB-": ["AB-"],
    "AB+": ["AB-", "AB+"],
}

# Plasma compatibility is inverted by ABO
# Recipient ABO -> allowed donor ABOs
PLASMA_MATRIX = {
    "O": ["O", "A", "B", "AB"],
    "A": ["A", "AB"],
    "B": ["B", "AB"],
    "AB": ["AB"],
}


def facility(
    code: str,
    name: str,
    facility_type: str,
    district: str,
    division: str,
    latitude: float,
    longitude: float,
    bed_count: int,
    has_trauma_centre: bool = False,
    has_oncology: bool = False,
    has_thalassaemia_centre: bool = False,
    has_cardiac_surgery: bool = False,
    has_obgyn: bool = True,
):
    return {
        "code": code,
        "name_en": name,
        "facility_type": facility_type,
        "district": district,
        "division": division,
        "latitude": latitude,
        "longitude": longitude,
        "bed_count": bed_count,
        "has_trauma_centre": has_trauma_centre,
        "has_oncology": has_oncology,
        "has_thalassaemia_centre": has_thalassaemia_centre,
        "has_cardiac_surgery": has_cardiac_surgery,
        "has_obgyn": has_obgyn,
    }


FACILITIES = [
    facility("RBC_LAHORE", "Regional Blood Centre Lahore", "RBC", "Lahore", "Lahore", 31.5497, 74.3436, 0, has_obgyn=False),
    facility("RBC_MULTAN", "Regional Blood Centre Multan", "RBC", "Multan", "Multan", 30.1980, 71.4680, 0, has_obgyn=False),

    facility("SERVICES_LAHORE", "Services Hospital Lahore", "TERTIARY_HOSPITAL", "Lahore", "Lahore", 31.5709, 74.3102, 1200, has_trauma_centre=True),
    facility("MAYO_LAHORE", "Mayo Hospital Lahore", "TERTIARY_HOSPITAL", "Lahore", "Lahore", 31.5670, 74.3000, 1500, has_trauma_centre=True, has_oncology=True),
    facility("JINNAH_LAHORE", "Jinnah Hospital Lahore", "TERTIARY_HOSPITAL", "Lahore", "Lahore", 31.4940, 74.3240, 1000, has_trauma_centre=True),
    facility("GENERAL_LAHORE", "Lahore General Hospital", "TERTIARY_HOSPITAL", "Lahore", "Lahore", 31.5150, 74.3830, 800, has_trauma_centre=True),
    facility("CHILDREN_LAHORE", "Children's Hospital Lahore", "SPECIALIST_CENTRE", "Lahore", "Lahore", 31.5790, 74.3360, 600, has_oncology=True, has_thalassaemia_centre=True, has_obgyn=False),
    facility("SHKAT_LAHORE", "Shaukat Khanum Memorial Cancer Hospital Lahore", "SPECIALIST_CENTRE", "Lahore", "Lahore", 31.4620, 74.3820, 200, has_oncology=True, has_obgyn=False),
    facility("GULAB_LAHORE", "Gulab Devi Hospital Lahore", "TERTIARY_HOSPITAL", "Lahore", "Lahore", 31.5860, 74.3870, 800),
    facility("SHEIKH_ZAYED_LHR", "Sheikh Zayed Hospital Lahore", "TERTIARY_HOSPITAL", "Lahore", "Lahore", 31.5230, 74.3620, 700),

    facility("DHQ_KASUR", "DHQ Kasur", "DHQ", "Kasur", "Lahore", 31.1180, 74.4500, 300),
    facility("DHQ_SHEIKHUPURA", "DHQ Sheikhupura", "DHQ", "Sheikhupura", "Lahore", 31.7080, 74.0000, 300),
    facility("THQ_FEROZEWALA", "THQ Ferozewala", "THQ", "Sheikhupura", "Lahore", 31.6320, 74.0640, 100),

    facility("BBH_RAWALPINDI", "Benazir Bhutto Hospital Rawalpindi", "TERTIARY_HOSPITAL", "Rawalpindi", "Rawalpindi", 33.6000, 73.0500, 1200, has_trauma_centre=True),
    facility("HOLY_RAWALPINDI", "Holy Family Hospital Rawalpindi", "TERTIARY_HOSPITAL", "Rawalpindi", "Rawalpindi", 33.6040, 73.0580, 900),
    facility("DHQ_JHELUM", "DHQ Jhelum", "DHQ", "Jhelum", "Rawalpindi", 32.9350, 73.7310, 300),

    facility("ALLIED_FAISALABAD", "Allied Hospital Faisalabad", "TERTIARY_HOSPITAL", "Faisalabad", "Faisalabad", 31.4180, 73.0790, 1400, has_trauma_centre=True),
    facility("DHQ_FAISALABAD", "DHQ Faisalabad", "DHQ", "Faisalabad", "Faisalabad", 31.4500, 73.1000, 400),
    facility("DHQ_JHANG", "DHQ Jhang", "DHQ", "Jhang", "Faisalabad", 31.2680, 72.3180, 300),

    facility("NISHTAR_MULTAN", "Nishtar Hospital Multan", "TERTIARY_HOSPITAL", "Multan", "Multan", 30.1950, 71.4700, 1500, has_trauma_centre=True, has_oncology=True),
    facility("DHQ_KHANEWAL", "DHQ Khanewal", "DHQ", "Khanewal", "Multan", 30.3040, 71.9360, 250),
    facility("DHQ_VEHARI", "DHQ Vehari", "DHQ", "Vehari", "Multan", 30.0440, 72.3520, 250),

    facility("DHQ_GUJRANWALA", "DHQ Gujranwala", "DHQ", "Gujranwala", "Gujranwala", 32.1870, 74.1940, 400),
    facility("DHQ_SIALKOT", "DHQ Sialkot", "DHQ", "Sialkot", "Gujranwala", 32.4940, 74.5270, 400),

    facility("DHQ_SARGODHA", "DHQ Sargodha", "DHQ", "Sargodha", "Sargodha", 32.0830, 72.6710, 400),
    facility("DHQ_BHAKKAR", "DHQ Bhakkar", "DHQ", "Bhakkar", "Sargodha", 31.6270, 71.0630, 250),

    facility("BVH_BAHAWALPUR", "Bahawal Victoria Hospital Bahawalpur", "TERTIARY_HOSPITAL", "Bahawalpur", "Bahawalpur", 29.3950, 71.6830, 900, has_trauma_centre=True),
    facility("DHQ_RAHIMYARHAN", "DHQ Rahim Yar Khan", "DHQ", "Rahim Yar Khan", "Bahawalpur", 28.4200, 70.2950, 400),
    facility("DHQ_DGKHAN", "DHQ D.G. Khan", "DHQ", "D.G. Khan", "D.G. Khan", 30.0530, 70.6380, 350),
    facility("DHQ_SAHIVAL", "DHQ Sahiwal", "DHQ", "Sahiwal", "Sahiwal", 30.6700, 73.1060, 350),
]


def seed_blood_groups(session):
    for code, abo, rh, pct in BLOOD_GROUPS:
        existing = session.scalar(
            select(BloodGroup).where(BloodGroup.code == code)
        )
        if existing is None:
            session.add(
                BloodGroup(
                    code=code,
                    abo=abo,
                    rh=rh,
                    population_pct_pk=pct,
                )
            )

    session.flush()
    groups = session.scalars(select(BloodGroup)).all()
    return {group.code: group for group in groups}


def seed_components(session):
    for (
        code,
        name_en,
        name_ur,
        shelf_life_days,
        temp_min,
        temp_max,
        requires_agitation,
        max_transport_hours,
        criticality_weight,
    ) in COMPONENTS:
        existing = session.scalar(
            select(Component).where(Component.code == code)
        )
        if existing is None:
            session.add(
                Component(
                    code=code,
                    name_en=name_en,
                    name_ur=name_ur,
                    shelf_life_days=shelf_life_days,
                    storage_temp_min_c=temp_min,
                    storage_temp_max_c=temp_max,
                    requires_agitation=requires_agitation,
                    max_transport_hours=max_transport_hours,
                    criticality_weight=criticality_weight,
                )
            )

    session.flush()
    components = session.scalars(select(Component)).all()
    return {component.code: component for component in components}


def seed_facilities(session, groups):
    """Create or refresh every facility.

    Policy fields are rewritten on every run, not skipped when the facility
    already exists. A reserve policy that can only be set at first insert is a
    policy that cannot be corrected.
    """

    group_shares = build_group_shares(list(groups.values()))

    for item in FACILITIES:
        capacity, reserve_totals, operating = facility_policy(item["facility_type"])

        # Stored as component -> group -> units. A single number per component
        # cannot be applied per series without either multiplying the facility's
        # obligation eightfold or diluting O- to a fraction of a unit.
        reserve = expand_reserve_policy(
            reserve_totals,
            item["facility_type"],
            group_shares,
        )

        existing = session.scalar(
            select(Facility).where(Facility.code == item["code"])
        )

        if existing is not None:
            existing.storage_capacity_json = capacity
            existing.min_reserve_policy_json = reserve
            existing.operating_hours_json = operating
            continue

        session.add(
            Facility(
                code=item["code"],
                name_en=item["name_en"],
                name_ur=None,
                facility_type=item["facility_type"],
                district=item["district"],
                division=item["division"],
                province="Punjab",
                latitude=item["latitude"],
                longitude=item["longitude"],
                bed_count=item["bed_count"],
                has_trauma_centre=item["has_trauma_centre"],
                has_obgyn=item["has_obgyn"],
                has_oncology=item["has_oncology"],
                has_thalassaemia_centre=item["has_thalassaemia_centre"],
                has_cardiac_surgery=item["has_cardiac_surgery"],
                storage_capacity_json=capacity,
                min_reserve_policy_json=reserve,
                operating_hours_json=operating,
                integration_mode="SIMULATED",
                is_active=True,
            )
        )

    session.flush()
    link_hub_and_spoke(session)


def link_hub_and_spoke(session):
    facilities = session.scalars(select(Facility)).all()
    by_code = {facility.code: facility for facility in facilities}

    for facility in facilities:
        if facility.facility_type == "RBC":
            facility.parent_rbc_id = None
            continue

        hub_code = DIVISION_TO_RBC.get(facility.division or "")
        hub = by_code.get(hub_code) if hub_code else None

        facility.parent_rbc_id = hub.id if hub else None

    session.flush()


def get_compatibility(component_code: str, recipient: BloodGroup, donor: BloodGroup):
    """Return (is_compatible, preference_rank, requires_override).

    Seeded from the matrices in spec §19.1. Red cell and plasma compatibility
    are inverted with respect to each other, which is why both are seeded from
    data and neither lives in optimizer logic.
    """

    if component_code in WHOLE_BLOOD_COMPONENTS:
        allowed = WHOLE_BLOOD_MATRIX.get(recipient.code, [])

        if donor.code not in allowed:
            return False, 9, False

        if donor.code == recipient.code:
            return True, 1, False

        # Rh-negative into an Rh-positive recipient: acceptable, but second
        # choice because it spends a scarce Rh-negative unit.
        return True, 2, False

    if component_code in RBC_COMPONENTS:
        allowed = RBC_MATRIX.get(recipient.code, [])
        if donor.code not in allowed:
            return False, 9, False

        if donor.code == recipient.code:
            return True, 1, False

        if (
            donor.abo == recipient.abo
            and donor.rh == "-"
            and recipient.rh == "+"
        ):
            return True, 2, False

        return True, 3, False

    if component_code in PLASMA_COMPONENTS:
        allowed_abos = PLASMA_MATRIX.get(recipient.abo, [])
        if donor.abo not in allowed_abos:
            return False, 9, False

        if donor.code == recipient.code:
            return True, 1, False

        # Rh is generally not a barrier for plasma (spec §19.1).
        if donor.abo == recipient.abo:
            return True, 2, False

        return True, 3, False

    if component_code in PLATELET_COMPONENTS:
        if donor.code == recipient.code:
            return True, 1, False

        # Same ABO, different Rh: a soft preference, not a barrier. Rh-negative
        # platelets are preferred for Rh-negative women of childbearing
        # potential, which the optimizer expresses as a substitution penalty.
        if donor.abo == recipient.abo:
            return True, 2, False

        # ABO-incompatible platelets may be issued in shortage with volume
        # reduction, but only under an explicit clinical override. Marking these
        # plainly compatible would let a routine plan ship AB+ platelets to an
        # O- patient for the price of a substitution penalty.
        return True, 3, True

    return False, 9, False


def seed_compatibility(session, components, groups):
    session.query(Compatibility).delete(synchronize_session=False)
    session.flush()

    rows = []

    for component in components.values():
        for recipient in groups.values():
            for donor in groups.values():
                is_compatible, preference_rank, requires_override = (
                    get_compatibility(
                        component.code,
                        recipient,
                        donor,
                    )
                )

                rows.append(
                    Compatibility(
                        component_id=component.id,
                        recipient_group_id=recipient.id,
                        donor_group_id=donor.id,
                        is_compatible=is_compatible,
                        preference_rank=preference_rank,
                        requires_override=requires_override,
                    )
                )

    session.add_all(rows)
    session.flush()


def print_counts(session):
    blood_groups = session.scalar(
        select(func.count()).select_from(BloodGroup)
    )
    components = session.scalar(
        select(func.count()).select_from(Component)
    )
    compatibility_rows = session.scalar(
        select(func.count()).select_from(Compatibility)
    )
    facilities = session.scalar(
        select(func.count()).select_from(Facility)
    )

    print("Seed complete.")
    print(f"Blood groups: {blood_groups}")
    print(f"Components: {components}")
    print(f"Compatibility rows: {compatibility_rows}")
    print(f"Facilities: {facilities}")


def main():
    init_db()
    session = SessionLocal()

    try:
        groups = seed_blood_groups(session)
        components = seed_components(session)
        seed_facilities(session, groups)
        seed_compatibility(session, components, groups)

        session.commit()
        print_counts(session)

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


if __name__ == "__main__":
    main()
