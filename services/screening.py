"""Chair-side screening and collection: the first two steps of the chain.

Everything that writes goes through here rather than through a route, for two
reasons. A route guard protects one entry point and these operations will
eventually have several — the camp tablet, an import, a correction screen. And
the clinical rules belong next to the write they gate, not in a template.

Three rules this module will not let a caller past:

1. **A deferred donor is not bled.** `record_donation` refuses unless the
   screening it cites recorded an ACCEPTED outcome. Not "the UI hides the
   button" — the function refuses.

2. **Volume follows weight.** The two-step ladder in `core.eligibility` decides
   how much may be taken, and below the absolute floor nothing may be.

3. **A contested rule is not decided by a phlebotomist.** Seven rules in
   `config/network.yaml` are flagged as needing clinical sign-off because the
   sources disagree. When one fires, the deferral stands and the case is queued
   for someone with the authority to weigh it. Nobody at the chair can override.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Permission
from core import config, eligibility
from core.clock import DEMO_DATETIME
from db.models import (
    BloodUnit,
    Component,
    Donation,
    DonationSession,
    Donor,
    DonorDeferral,
    DonorScreening,
    Facility,
)
from services.audit import Actor, ServiceError, audited, require, snapshot

# The register accepts these ages. Donors outside the range may be
# registered — people age out, and a 17-year-old may pre-register — but
# none of them will pass screening.
AGE_MIN = int(config.get("donor_eligibility.age_years_min"))
AGE_MAX = int(config.get("donor_eligibility.age_years_max"))

SCREENING_FIELDS = (
    "donor_id",
    "facility_id",
    "session_id",
    "screened_at",
    "haemoglobin_g_dl",
    "weight_kg",
    "systolic_bp",
    "diastolic_bp",
    "pulse_bpm",
    "temperature_c",
    "outcome",
    "deferral_reason_code",
    "deferral_days",
    "screened_by",
)

DONATION_FIELDS = (
    "din",
    "donor_id",
    "screening_id",
    "facility_id",
    "collected_at",
    "donation_type",
    "bag_type",
    "volume_ml",
    "status",
    "phlebotomist",
    "adverse_reaction",
)


def contested_rules() -> set[str]:
    """Reason codes the configuration says need a clinician, not a default.

    These are the rules where the Punjab SOP and WHO disagree, or where the SOP
    contradicts itself. The config records both readings and which one is
    applied; what it cannot do is make the choice for a particular donor.
    """

    return set(config.get("requires_clinical_signoff") or {})


@dataclass
class Verdict:
    """What the engine concluded, in the shape a screen needs."""

    outcome: str
    assessment: eligibility.Assessment
    needs_signoff: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.outcome == "ACCEPTED"

    @property
    def primary_reason(self) -> str | None:
        return self.assessment.deferrals[0].reason_code if self.assessment.deferrals else None

    @property
    def collection_volume_ml(self) -> int | None:
        return self.assessment.collection_volume_ml


def _sex(donor: Donor) -> str:
    """Unknown sex takes the stricter profile, never the more permissive one."""

    return "FEMALE" if (donor.gender or "").upper() == "FEMALE" else "MALE"


def _age(donor: Donor, today: date) -> int | None:
    born = donor.date_of_birth

    if born is None:
        return None

    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def assess(
    donor: Donor,
    *,
    today: date | None = None,
    haemoglobin_g_dl: float | None = None,
    weight_kg: float | None = None,
    systolic_bp: int | None = None,
    diastolic_bp: int | None = None,
    pulse_bpm: int | None = None,
    temperature_c: float | None = None,
    answers: dict | None = None,
) -> Verdict:
    """Assess this donor against everything known so far.

    Safe to call with partial input — the wizard calls it after each step so the
    verdict is live. A donor deferred on their weight should learn that before
    answering twelve more questions.
    """

    today = today or DEMO_DATETIME.date()
    last = donor.last_donation_at

    assessment = eligibility.assess(
        sex=_sex(donor),
        today=today,
        age_years=_age(donor, today),
        haemoglobin_g_dl=haemoglobin_g_dl,
        weight_kg=weight_kg,
        systolic_bp=systolic_bp,
        diastolic_bp=diastolic_bp,
        pulse_bpm=pulse_bpm,
        temperature_c=temperature_c,
        last_donation_on=last.date() if last else None,
        answers=answers or {},
        already_permanently_deferred=bool(donor.is_permanently_deferred),
        deferred_until=donor.deferred_until,
    )

    contested = contested_rules()
    needs_signoff = [
        deferral.reason_code
        for deferral in assessment.deferrals
        if deferral.reason_code in contested
    ]

    return Verdict(
        outcome=assessment.outcome,
        assessment=assessment,
        needs_signoff=needs_signoff,
    )


def record_screening(
    db: Session,
    actor: Actor,
    *,
    donor_id: str,
    session_id: str | None,
    vitals: dict,
    answers: dict | None = None,
    notes: str | None = None,
) -> DonorScreening:
    """Record a screening and, if it defers, the deferral that follows from it.

    A deferral written here is a real one: it lands in `donor_deferral` with its
    kind, and the donor's status moves with it. The register reads that ledger,
    so a donor deferred at the chair stops appearing on the recall list
    immediately rather than at the next pipeline run.
    """

    require(actor, Permission.SCREEN_DONOR, "screen donors")

    donor = db.get(Donor, donor_id)

    if donor is None:
        raise ServiceError("DONOR_NOT_FOUND", "That donor is not on the register.")

    if actor.facility_id and donor.registered_facility_id != actor.facility_id:
        # Same shape as the register's guard: a donor from another facility is
        # not visible, so screening one is not possible either.
        raise ServiceError("DONOR_NOT_FOUND", "That donor is not on the register.")

    today = DEMO_DATETIME.date()
    verdict = assess(donor, today=today, answers=answers, **vitals)

    screening = DonorScreening(
        id=str(uuid.uuid4()),
        donor_id=donor_id,
        session_id=session_id,
        facility_id=donor.registered_facility_id,
        screened_at=DEMO_DATETIME,
        questionnaire_json=answers or {},
        outcome="ACCEPTED" if verdict.accepted else "DEFERRED",
        deferral_reason_code=verdict.primary_reason,
        screened_by=actor.display_name,
        notes=notes,
        **vitals,
    )

    with audited(db, actor, "DONOR_SCREENED", "donor_screening") as entry:
        db.add(screening)
        db.flush()

        deferral = _deferral_from(verdict, donor_id=donor_id, actor=actor)

        if deferral is not None:
            screening.deferral_days = deferral["days"]
            db.add(DonorDeferral(**deferral["row"]))
            _apply_to_donor(donor, deferral)

        entry.on(screening, after=snapshot(screening, SCREENING_FIELDS))
        entry.note(
            outcome=verdict.outcome,
            deferrals=[d.reason_code for d in verdict.assessment.deferrals],
            needs_clinical_signoff=verdict.needs_signoff,
            collection_volume_ml=verdict.collection_volume_ml,
        )

    return screening


def _deferral_from(verdict: Verdict, *, donor_id: str, actor: Actor) -> dict | None:
    """Turn the engine's verdict into a deferral row, honouring its kind.

    Only a TIMED deferral gets an end date. Giving one to a CONDITIONAL or a
    PERMANENT deferral would let `today >= deferred_until` score the donor
    eligible on a date that means nothing — the mistake the kind enum exists to
    prevent.
    """

    if verdict.accepted or not verdict.assessment.deferrals:
        return None

    worst = verdict.assessment.deferrals[0]
    kind = str(worst.kind).upper()
    contested = worst.reason_code in contested_rules()

    days = int(worst.days) if kind == "TIMED" and worst.days else None
    until = (DEMO_DATETIME.date() + timedelta(days=days)) if days else None

    note = worst.detail or None

    if contested:
        note = (
            (note + " " if note else "")
            + "This rule is flagged for clinical sign-off: the sources disagree "
            "and the deferral stands until a clinician reviews it."
        )

    return {
        "days": days,
        "kind": kind,
        "contested": contested,
        "row": {
            "id": str(uuid.uuid4()),
            "donor_id": donor_id,
            "deferred_at": DEMO_DATETIME,
            "deferred_until": until,
            "is_permanent": kind == "PERMANENT",
            "reason_code": worst.reason_code,
            "reason_note": note,
            "recorded_by": actor.display_name,
        },
    }


def _apply_to_donor(donor: Donor, deferral: dict) -> None:
    """Keep the donor's denormalised status in step with the ledger."""

    kind = deferral["kind"]
    row = deferral["row"]

    if kind == "PERMANENT":
        donor.is_permanently_deferred = True
        donor.deferred_until = None
        donor.availability_status = "PERMANENTLY_DEFERRED"
    elif row["deferred_until"] is not None:
        donor.deferred_until = row["deferred_until"]
        donor.availability_status = "TEMPORARILY_DEFERRED"
    else:
        donor.deferred_until = None
        donor.availability_status = "CONDITIONALLY_DEFERRED"


def record_donation(
    db: Session,
    actor: Actor,
    *,
    screening_id: str,
    donation_type: str = "WHOLE_BLOOD",
    bag_type: str = "TRIPLE",
    adverse_reaction: str | None = None,
    volume_ml: int | None = None,
) -> Donation:
    """Record a collection, and the bag it produced.

    The unit row is created here, at QUARANTINE. The bag physically exists the
    moment it is drawn, so the record does too — but it is not issuable and is
    not counted as available stock until the lab releases it. Creating the unit
    only on release would leave blood in the building with no record, and would
    give a reactive result nothing to discard.
    """

    require(actor, Permission.COLLECT_DONATION, "record a collection")

    screening = db.get(DonorScreening, screening_id)

    if screening is None:
        raise ServiceError("SCREENING_NOT_FOUND", "That screening does not exist.")

    if screening.outcome != "ACCEPTED":
        # The refusal lives here, not in the template. A hidden button is not a
        # control.
        raise ServiceError(
            "DONOR_DEFERRED",
            "This donor was deferred at screening and must not be bled.",
        )

    existing = db.scalar(
        select(Donation.id).where(Donation.screening_id == screening_id)
    )

    if existing:
        raise ServiceError(
            "ALREADY_COLLECTED",
            "A donation has already been recorded against this screening.",
        )

    permitted = eligibility.collection_volume_for(screening.weight_kg)

    if permitted is None:
        raise ServiceError(
            "BELOW_WEIGHT_FLOOR",
            "This donor is below the minimum weight for any collection.",
            field="weight_kg",
        )

    if donation_type == "APHERESIS":
        volume = volume_ml or 250
    else:
        volume = min(volume_ml or permitted, permitted)

        if volume < permitted:
            # A short draw is allowed and recorded; over-drawing is not.
            pass

    donation = Donation(
        id=str(uuid.uuid4()),
        din=_next_din(db, screening.facility_id),
        donor_id=screening.donor_id,
        session_id=screening.session_id,
        screening_id=screening_id,
        facility_id=screening.facility_id,
        is_directed=False,
        collected_at=DEMO_DATETIME,
        donation_type=donation_type,
        bag_type=bag_type,
        anticoagulant="CPDA-1" if donation_type != "APHERESIS" else "ACD-A",
        volume_ml=volume,
        status="COLLECTED",
        grouping_discrepancy=False,
        adverse_reaction=adverse_reaction,
        phlebotomist=actor.display_name,
        created_at=DEMO_DATETIME,
    )

    with audited(db, actor, "DONATION_COLLECTED", "donation") as entry:
        db.add(donation)
        db.flush()

        donor = db.get(Donor, screening.donor_id)
        units = _quarantine_units(db, donation, donor)

        for unit in units:
            db.add(unit)

        donor.last_donation_at = DEMO_DATETIME
        donor.total_donations = (donor.total_donations or 0) + 1
        donor.availability_status = "RECENTLY_DONATED"

        if donor.first_donation_at is None:
            donor.first_donation_at = DEMO_DATETIME

        entry.on(donation, after=snapshot(donation, DONATION_FIELDS))
        entry.note(
            units_created=len(units),
            unit_dins=[unit.din for unit in units],
            unit_status="QUARANTINE",
            permitted_volume_ml=permitted,
        )

    return donation


def _quarantine_units(db: Session, donation: Donation, donor: Donor) -> list[BloodUnit]:
    """The bag itself, as one unit record.

    ONE unit, not one per component. A collection produces a single physical bag;
    the products it separates into do not exist until somebody spins it, which is
    what `services.processing` records.

    Creating the components here was wrong in two ways. It asserted a separation
    that had not happened — so a bag sitting unspun in the fridge appeared in
    inventory as three finished products — and it made the yield report
    meaningless, because expected and produced were equal by construction.

    The unit is whole blood and it is quarantined. After release it is issuable
    AS whole blood, which is a real thing to do with it, or it can be separated.
    """

    component = db.scalars(
        select(Component).where(Component.code == "WB")
    ).first()

    if component is None:
        return []

    return [
        BloodUnit(
            id=str(uuid.uuid4()),
            din=donation.din,
            donation_id=donation.id,
            facility_id=donation.facility_id,
            component_id=component.id,
            blood_group_id=donor.blood_group_id if donor else None,
            volume_ml=donation.volume_ml,
            collected_at=donation.collected_at,
            expires_at=donation.collected_at
            + timedelta(days=int(component.shelf_life_days)),
            # Not issuable, and not counted as stock, until the lab releases it.
            status="QUARANTINE",
            screening_status="PENDING",
            is_leucodepleted=False,
            is_irradiated=False,
            cold_chain_breach_count=0,
            last_synced_at=DEMO_DATETIME,
        )
    ]


def _next_din(db: Session, facility_id: str) -> str:
    """Allocate the next Donation Identification Number for this facility.

    Sequence is per (FIN, year), which is the only uniqueness rule ISBT 128
    imposes. The loop guards against a gap left by a rolled-back transaction:
    counting rows gives a candidate, and a collision just takes the next one.
    """

    from sqlalchemy import func

    from core import isbt
    from db.models import Facility

    # configured_fin keys on the facility CODE, not its id. Passing the UUID
    # matched nothing and dropped every facility onto the shared Z0000
    # placeholder — and because ISBT uniqueness is per (FIN, year) while the
    # counter below is per facility, two facilities collecting on the same day
    # would have minted the same identifier.
    code = db.scalar(select(Facility.code).where(Facility.id == facility_id))
    fin, provisional = isbt.configured_fin(code)

    # Counted across every facility sharing this FIN, which is the scope the
    # standard's uniqueness rule actually has.
    sequence = (
        db.scalar(
            select(func.count())
            .select_from(Donation)
            .join(Facility, Facility.id == Donation.facility_id)
            .where(Facility.code == code)
        )
        or 0
    ) + 1

    while True:
        identifier = isbt.build_din(
            sequence=sequence,
            year=DEMO_DATETIME.year,
            fin=fin,
            provisional=provisional,
        )

        if not db.scalar(select(Donation.id).where(Donation.din == identifier.din)):
            return identifier.din

        sequence += 1


# --------------------------------------------------------------------- drafts
#
# A screening row is created when the donor is identified and updated as the
# wizard progresses, so a closed tab or a flat tablet battery does not lose the
# work. The cost is that DRAFT rows sit in a table other things count, and every
# such count has to exclude them — asserted in the tests rather than left to
# whoever writes the next query.

DRAFT = "DRAFT"
ABANDONED = "ABANDONED"

# Outcomes representing a screening that actually happened.
FINAL_OUTCOMES = ("ACCEPTED", "DEFERRED")


def start_screening(
    db: Session, actor: Actor, *, donor_id: str, session_id: str
) -> DonorScreening:
    """Open a draft the wizard fills in step by step.

    Returns the donor's existing open draft for this session if there is one, so
    reopening the wizard resumes rather than starting a second record.
    """

    require(actor, Permission.SCREEN_DONOR, "screen donors")

    donor = _own_donor(db, actor, donor_id)

    existing = db.scalars(
        select(DonorScreening).where(
            DonorScreening.donor_id == donor_id,
            DonorScreening.session_id == session_id,
            DonorScreening.outcome == DRAFT,
        )
    ).first()

    if existing is not None:
        return existing

    record = DonorScreening(
        id=str(uuid.uuid4()),
        donor_id=donor_id,
        session_id=session_id,
        facility_id=donor.registered_facility_id,
        screened_at=DEMO_DATETIME,
        questionnaire_json={},
        outcome=DRAFT,
        screened_by=actor.display_name,
    )

    with audited(db, actor, "SCREENING_STARTED", "donor_screening") as entry:
        db.add(record)
        db.flush()
        entry.on(record, after=snapshot(record, SCREENING_FIELDS))

    return record


def save_draft(
    db: Session,
    actor: Actor,
    *,
    screening_id: str,
    vitals: dict | None = None,
    answers: dict | None = None,
) -> tuple[DonorScreening, Verdict]:
    """Save a step and return the verdict as it now stands.

    The engine produces the verdict on every save, so the chair sees a deferral
    the moment the haemoglobin is entered rather than after twelve more
    questions — and no rule is duplicated into the browser to achieve it.
    """

    require(actor, Permission.SCREEN_DONOR, "screen donors")

    record = _own_draft(db, actor, screening_id)
    donor = db.get(Donor, record.donor_id)

    with audited(
        db, actor, "SCREENING_UPDATED", "donor_screening", screening_id
    ) as entry:
        before = snapshot(record, SCREENING_FIELDS)

        for field_name, value in (vitals or {}).items():
            if field_name in SCREENING_FIELDS:
                setattr(record, field_name, value)

        if answers is not None:
            merged = dict(record.questionnaire_json or {})
            merged.update(answers)
            record.questionnaire_json = merged

        db.flush()
        entry.on(record, before=before, after=snapshot(record, SCREENING_FIELDS))

    return record, current_verdict(db, record, donor)


def current_verdict(
    db: Session, record: DonorScreening, donor: Donor | None = None
) -> Verdict:
    """Assess a draft against whatever has been entered so far."""

    donor = donor or db.get(Donor, record.donor_id)

    return assess(
        donor,
        haemoglobin_g_dl=record.haemoglobin_g_dl,
        weight_kg=record.weight_kg,
        systolic_bp=record.systolic_bp,
        diastolic_bp=record.diastolic_bp,
        pulse_bpm=record.pulse_bpm,
        temperature_c=record.temperature_c,
        answers=record.questionnaire_json or {},
    )


def finalise_screening(
    db: Session, actor: Actor, *, screening_id: str, notes: str | None = None
) -> tuple[DonorScreening, Verdict]:
    """Commit a draft as a real screening, with the deferral it implies."""

    require(actor, Permission.SCREEN_DONOR, "screen donors")

    record = _own_draft(db, actor, screening_id)
    donor = db.get(Donor, record.donor_id)

    missing = [
        name
        for name in ("haemoglobin_g_dl", "weight_kg")
        if getattr(record, name) is None
    ]

    if missing:
        raise ServiceError(
            "INCOMPLETE",
            "Haemoglobin and weight are needed before a screening can be "
            "completed - they decide both eligibility and how much may be taken.",
            field=missing[0],
        )

    verdict = current_verdict(db, record, donor)
    before = snapshot(record, SCREENING_FIELDS)

    with audited(
        db, actor, "DONOR_SCREENED", "donor_screening", screening_id
    ) as entry:
        record.outcome = "ACCEPTED" if verdict.accepted else "DEFERRED"
        record.deferral_reason_code = verdict.primary_reason
        record.screened_at = DEMO_DATETIME

        if notes:
            record.notes = notes

        deferral = _deferral_from(verdict, donor_id=record.donor_id, actor=actor)

        if deferral is not None:
            record.deferral_days = deferral["days"]
            db.add(DonorDeferral(**deferral["row"]))
            _apply_to_donor(donor, deferral)

        db.flush()

        entry.on(record, before=before, after=snapshot(record, SCREENING_FIELDS))
        entry.note(
            outcome=verdict.outcome,
            deferrals=[d.reason_code for d in verdict.assessment.deferrals],
            needs_clinical_signoff=verdict.needs_signoff,
            collection_volume_ml=verdict.collection_volume_ml,
        )

    return record, verdict


def abandon_draft(
    db: Session, actor: Actor, *, screening_id: str, reason: str | None = None
) -> DonorScreening:
    """Mark a draft abandoned rather than deleting it.

    A donor who presented and walked off is a real event, and at a camp the
    reason is often the useful part. Deleting the row loses it.
    """

    require(actor, Permission.SCREEN_DONOR, "screen donors")

    record = _own_draft(db, actor, screening_id)
    before = snapshot(record, SCREENING_FIELDS)

    with audited(
        db, actor, "SCREENING_ABANDONED", "donor_screening", screening_id
    ) as entry:
        record.outcome = ABANDONED
        record.notes = reason or record.notes
        db.flush()
        entry.on(record, before=before, after=snapshot(record, SCREENING_FIELDS))
        entry.note(reason=reason)

    return record


def open_drafts(db: Session, *, session_id: str) -> list:
    """Unfinished screenings on a session, so the next person can resume one."""

    return list(
        db.scalars(
            select(DonorScreening)
            .where(
                DonorScreening.session_id == session_id,
                DonorScreening.outcome == DRAFT,
            )
            .order_by(DonorScreening.screened_at)
        ).all()
    )


def _own_donor(db: Session, actor: Actor, donor_id: str) -> Donor:
    donor = db.get(Donor, donor_id)

    if donor is None or (
        actor.facility_id and donor.registered_facility_id != actor.facility_id
    ):
        raise ServiceError("DONOR_NOT_FOUND", "That donor is not on the register.")

    return donor


def _own_draft(db: Session, actor: Actor, screening_id: str) -> DonorScreening:
    record = db.scalars(
        select(DonorScreening).where(
            DonorScreening.id == screening_id,
            DonorScreening.facility_id == actor.facility_id,
        )
    ).first()

    if record is None:
        raise ServiceError("SCREENING_NOT_FOUND", "That screening does not exist here.")

    if record.outcome != DRAFT:
        raise ServiceError(
            "ALREADY_COMPLETE",
            "That screening has already been completed and cannot be changed.",
        )

    return record


# ------------------------------------------------------------- registration
#
# At a camp most donors are not on the register yet, and sending somebody away
# to fill in a form elsewhere loses the donation. This captures only what
# screening needs; address and contact preferences can be completed later from
# the donor record.

REGISTRATION_FIELDS = (
    "donor_code",
    "full_name",
    "gender",
    "date_of_birth",
    "blood_group_id",
    "registered_facility_id",
    "donor_type",
    "consent_contact",
)


def register_donor(
    db: Session,
    actor: Actor,
    *,
    full_name: str,
    gender: str,
    date_of_birth: date,
    blood_group_id: int | None = None,
    phone: str | None = None,
    cnic_last4: str | None = None,
    donor_type: str = "REPLACEMENT",
    consent_contact: bool = False,
) -> Donor:
    """Add a donor to the register with the minimum screening needs."""

    require(actor, Permission.REGISTER_DONOR, "register donors")

    if not actor.facility_id:
        raise ServiceError("NO_FACILITY", "Select a facility before registering a donor.")

    full_name = (full_name or "").strip()

    if len(full_name) < 3:
        raise ServiceError("NAME_REQUIRED", "A donor needs a name.", field="full_name")

    if date_of_birth is None:
        raise ServiceError(
            "DOB_REQUIRED",
            "A date of birth is needed — age decides eligibility.",
            field="date_of_birth",
        )

    today = DEMO_DATETIME.date()
    age = (
        today.year
        - date_of_birth.year
        - ((today.month, today.day) < (date_of_birth.month, date_of_birth.day))
    )

    if age < 0 or age > 120:
        raise ServiceError(
            "DOB_IMPLAUSIBLE",
            "That date of birth cannot be right.",
            field="date_of_birth",
        )

    facility = db.get(Facility, actor.facility_id)

    record = Donor(
        id=str(uuid.uuid4()),
        donor_code=_next_donor_code(db),
        organization_id=facility.organization_id if facility else None,
        registered_facility_id=actor.facility_id,
        full_name=full_name,
        gender="FEMALE" if str(gender).upper() == "FEMALE" else "MALE",
        date_of_birth=date_of_birth,
        phone=(phone or None),
        # Only the last four are ever stored in clear. A full number would be
        # readable to anyone with database access, and matching does not need it.
        cnic_last4=(cnic_last4 or "").strip()[-4:] or None,
        blood_group_id=blood_group_id,
        blood_group_confirmed=False,
        city=facility.district if facility else None,
        district=facility.district if facility else None,
        donor_type=donor_type,
        availability_status="AVAILABLE",
        total_donations=0,
        is_permanently_deferred=False,
        consent_contact=bool(consent_contact),
        is_active=True,
        created_at=DEMO_DATETIME,
    )

    with audited(db, actor, "DONOR_REGISTERED", "donor") as entry:
        db.add(record)
        db.flush()
        entry.on(record, after=snapshot(record, REGISTRATION_FIELDS))
        entry.note(
            age_years=age,
            # Recorded because a donor registered outside the accepted range can
            # exist but will never pass screening, and somebody will ask why.
            within_age_range=AGE_MIN <= age <= AGE_MAX,
        )

    return record


def _next_donor_code(db: Session) -> str:
    """Sequential, readable, and unique. Codes are read out loud at a camp."""

    from sqlalchemy import func

    count = db.scalar(select(func.count()).select_from(Donor)) or 0
    candidate = count + 1

    while True:
        code = f"D-{candidate:06d}"

        if not db.scalar(select(Donor.id).where(Donor.donor_code == code)):
            return code

        candidate += 1
