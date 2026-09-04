from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from db.types import UtcDateTime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class BloodGroup(Base):
    __tablename__ = "blood_group"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(3), unique=True, index=True)
    abo: Mapped[str] = mapped_column(String(2))
    rh: Mapped[str] = mapped_column(String(1))
    population_pct_pk: Mapped[float] = mapped_column(Float, default=0.0)


class Component(Base):
    __tablename__ = "component"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    name_en: Mapped[str] = mapped_column(String(120))
    name_ur: Mapped[str | None] = mapped_column(String(200), nullable=True)
    shelf_life_days: Mapped[int] = mapped_column(Integer)
    storage_temp_min_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    storage_temp_max_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    requires_agitation: Mapped[bool] = mapped_column(Boolean, default=False)
    max_transport_hours: Mapped[float] = mapped_column(Float, default=24.0)
    criticality_weight: Mapped[float] = mapped_column(Float, default=1.0)


class Organization(Base):
    """A tenant: the body that owns one or more blood banks.

    Three tiers of visibility exist in this system, and this is the middle one.
    A facility does the operational work and sees all of its own. An organization
    sees everything across its own facilities. The network above sees only what a
    facility explicitly chooses to share, and never donor or patient records —
    that consent boundary is what makes cross-organization sharing acceptable to
    a hospital at all.
    """

    __tablename__ = "organization"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)

    name_en: Mapped[str] = mapped_column(String(255), index=True)
    name_ur: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # HOSPITAL_GROUP: a trust or chain with several facilities.
    # STANDALONE_HOSPITAL: a single hospital with one blood bank.
    # RBC_OPERATOR: runs a Regional Blood Centre and its spokes.
    # GOVT_PROGRAMME: a provincial or federal programme office.
    org_type: Mapped[str] = mapped_column(String(30), default="STANDALONE_HOSPITAL")

    province: Mapped[str] = mapped_column(String(120), default="Punjab")
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Opting in exposes this organization's shared facilities to network
    # availability search. Off by default: joining is a decision, not a default.
    network_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)

    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )


class UserAccount(Base):
    """A login. Scoped to one organization, optionally pinned to one facility.

    `facility_id` null means the user works across the organization's facilities
    (a group-level coordinator); set means they are staff at that blood bank.
    """

    __tablename__ = "user_account"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id"), index=True
    )
    facility_id: Mapped[str | None] = mapped_column(
        ForeignKey("facility.id"), nullable=True, index=True
    )

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(160))
    job_title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)

    role: Mapped[str] = mapped_column(String(40), default="BLOOD_BANK_OFFICER")

    # Personal interface state only: onboarding completion and future low-risk
    # display preferences. Clinical decisions, permissions, and facility scope
    # must never be stored here. Nullable keeps the additive SQLite migration
    # truthful for accounts created before this column existed.
    preferences_json: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=dict
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)

    # Rate limiting on credentials, per spec §13.2.
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )


class UserSession(Base):
    """Server-side session, so a logout or a lock actually ends access.

    Spec §13.2 requires a 30-minute session timeout. A signed cookie alone cannot
    be revoked, so the session record is the authority and the cookie only
    carries its id.
    """

    __tablename__ = "user_session"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), index=True)

    # The facility the user is currently working in. A group-level user switches
    # between their organization's facilities without re-authenticating.
    active_facility_id: Mapped[str | None] = mapped_column(
        ForeignKey("facility.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )

    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Facility(Base):
    __tablename__ = "facility"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(30), unique=True, index=True)

    organization_id: Mapped[str | None] = mapped_column(
        ForeignKey("organization.id"), nullable=True, index=True
    )

    name_en: Mapped[str] = mapped_column(String(255), index=True)
    name_ur: Mapped[str | None] = mapped_column(String(255), nullable=True)
    facility_type: Mapped[str] = mapped_column(String(30), default="HOSPITAL_BB")

    # Network sharing consent, per facility rather than per organization: a group
    # may publish stock from its teaching hospital and not from a private clinic.
    shares_inventory: Mapped[bool] = mapped_column(Boolean, default=False)
    shares_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    network_response_sla_minutes: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    parent_rbc_id: Mapped[str | None] = mapped_column(
        ForeignKey("facility.id"), nullable=True, index=True
    )
    district: Mapped[str] = mapped_column(String(120), index=True)
    division: Mapped[str | None] = mapped_column(String(120), nullable=True)
    province: Mapped[str] = mapped_column(String(120), default="Punjab")
    latitude: Mapped[float] = mapped_column(Float, default=31.5)
    longitude: Mapped[float] = mapped_column(Float, default=74.3)
    bed_count: Mapped[int] = mapped_column(Integer, default=100)

    has_trauma_centre: Mapped[bool] = mapped_column(Boolean, default=False)
    has_obgyn: Mapped[bool] = mapped_column(Boolean, default=True)
    has_oncology: Mapped[bool] = mapped_column(Boolean, default=False)
    has_thalassaemia_centre: Mapped[bool] = mapped_column(Boolean, default=False)
    has_cardiac_surgery: Mapped[bool] = mapped_column(Boolean, default=False)

    storage_capacity_json: Mapped[dict] = mapped_column(JSON, default=dict)
    min_reserve_policy_json: Mapped[dict] = mapped_column(JSON, default=dict)
    operating_hours_json: Mapped[dict] = mapped_column(JSON, default=dict)

    integration_mode: Mapped[str] = mapped_column(String(30), default="SIMULATED")

    # Existing installations pre-date the guided onboarding workflow, so NULL
    # is treated as ACTIVE. New facilities are created as DRAFT and remain
    # invisible to operational scopes until an administrator passes the
    # readiness gate and explicitly activates them.
    onboarding_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default="ACTIVE", index=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    activated_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Compatibility(Base):
    __tablename__ = "compatibility"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    component_id: Mapped[int] = mapped_column(
        ForeignKey("component.id", ondelete="CASCADE"), index=True
    )
    recipient_group_id: Mapped[int] = mapped_column(
        ForeignKey("blood_group.id", ondelete="CASCADE"), index=True
    )
    donor_group_id: Mapped[int] = mapped_column(
        ForeignKey("blood_group.id", ondelete="CASCADE"), index=True
    )
    is_compatible: Mapped[bool] = mapped_column(Boolean, default=False)
    preference_rank: Mapped[int] = mapped_column(Integer, default=3)

    # ABO-incompatible platelets may be issued in shortage with volume
    # reduction, but only under an explicit clinical override (spec §19.1).
    # Routine transfer plans must not select these pairs.
    requires_override: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint(
            "component_id",
            "recipient_group_id",
            "donor_group_id",
            name="uq_compatibility_component_recipient_donor",
        ),
    )


class Donor(Base):
    """A registered donor.

    Building the blood bank itself means holding donor identity, which the
    decision-layer spec (§13.2) was written to avoid. The CNIC is therefore
    stored as a salted hash with only the last four digits in clear: staff can
    confirm the person in front of them against a card, and a database copy does
    not hand over a national identity number.
    """

    __tablename__ = "donor"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    donor_code: Mapped[str] = mapped_column(String(30), unique=True, index=True)

    # Donors are registered by a facility but shared across the organization: a
    # hospital group runs one donor pool, not one per site.
    organization_id: Mapped[str | None] = mapped_column(
        ForeignKey("organization.id"), nullable=True, index=True
    )
    registered_facility_id: Mapped[str | None] = mapped_column(
        ForeignKey("facility.id"), nullable=True, index=True
    )

    full_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    cnic_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    cnic_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    gender: Mapped[str | None] = mapped_column(String(10), nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)

    blood_group_id: Mapped[int] = mapped_column(
        ForeignKey("blood_group.id"), index=True
    )
    # A first-time donor's group is unconfirmed until the lab types a donation.
    blood_group_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)

    city: Mapped[str] = mapped_column(String(120), index=True)
    district: Mapped[str] = mapped_column(String(120), index=True)
    age_band: Mapped[str | None] = mapped_column(String(20), nullable=True)

    donor_type: Mapped[str] = mapped_column(String(20), default="VOLUNTARY")

    availability_status: Mapped[str] = mapped_column(String(30), default="AVAILABLE")
    first_donation_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    last_donation_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    total_donations: Mapped[int] = mapped_column(Integer, default=0)

    # Denormalised from donor_deferral so the register can filter without a join.
    deferred_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_permanently_deferred: Mapped[bool] = mapped_column(Boolean, default=False)

    consent_contact: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )


class DonorDeferral(Base):
    """A period during which a donor must not donate.

    Temporary deferrals expire; a reactive screening result defers permanently.
    Keeping the history rather than only the current state matters, because a
    pattern of deferrals is clinically meaningful.
    """

    __tablename__ = "donor_deferral"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    donor_id: Mapped[str] = mapped_column(ForeignKey("donor.id"), index=True)

    deferred_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )
    deferred_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_permanent: Mapped[bool] = mapped_column(Boolean, default=False)

    # LOW_HAEMOGLOBIN, UNDERWEIGHT, HIGH_BP, LOW_BP, RECENT_DONATION,
    # RECENT_ILLNESS, MEDICATION, TATTOO_PIERCING, PREGNANCY, TRAVEL_MALARIA,
    # RISK_BEHAVIOUR, REACTIVE_SCREENING, OTHER
    reason_code: Mapped[str] = mapped_column(String(40))
    reason_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    recorded_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lifted_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    lifted_by: Mapped[str | None] = mapped_column(String(120), nullable=True)


class DonationSession(Base):
    """A collection event: an in-house clinic, an outdoor camp, or a mobile unit.

    Spec §15.3 treats camps as a distinct supply channel, and they behave
    differently: a camp brings whatever group mix walks in, which is one of the
    reasons stock and demand diverge.
    """

    __tablename__ = "donation_session"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_code: Mapped[str] = mapped_column(String(30), unique=True, index=True)

    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)
    organization_id: Mapped[str | None] = mapped_column(
        ForeignKey("organization.id"), nullable=True, index=True
    )

    session_type: Mapped[str] = mapped_column(String(20), default="IN_HOUSE")
    name: Mapped[str] = mapped_column(String(200))
    venue: Mapped[str | None] = mapped_column(String(255), nullable=True)
    district: Mapped[str | None] = mapped_column(String(120), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    scheduled_date: Mapped[date] = mapped_column(Date, index=True)
    opened_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )

    target_units: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="PLANNED", index=True)

    organiser: Mapped[str | None] = mapped_column(String(160), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )


class DonorScreening(Base):
    """Pre-donation assessment. Its outcome decides whether a donation happens."""

    __tablename__ = "donor_screening"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    donor_id: Mapped[str] = mapped_column(ForeignKey("donor.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("donation_session.id"), nullable=True, index=True
    )
    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)

    screened_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, index=True
    )

    haemoglobin_g_dl: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    systolic_bp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    diastolic_bp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pulse_bpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)

    questionnaire_json: Mapped[dict] = mapped_column(JSON, default=dict)

    outcome: Mapped[str] = mapped_column(String(20), default="ACCEPTED", index=True)
    deferral_reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    deferral_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    screened_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Donation(Base):
    """One donation by one donor: the head of the traceability chain.

    Every blood_unit produced points back here, and this points back to the
    donor, which is what makes ISBT-128 traceability (spec §1.3) real rather
    than nominal.
    """

    __tablename__ = "donation"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    # The Donation Identification Number is assigned at the chair and is the key
    # every downstream label and record carries.
    din: Mapped[str] = mapped_column(String(30), unique=True, index=True)

    donor_id: Mapped[str] = mapped_column(ForeignKey("donor.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("donation_session.id"), nullable=True, index=True
    )
    screening_id: Mapped[str | None] = mapped_column(
        ForeignKey("donor_screening.id"), nullable=True
    )
    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)

    # Replacement (family) donation is how most blood in Pakistan is actually
    # collected: a relative donates against a named patient's requirement and the
    # family is credited. Crucially this is a credit relationship, not a directed
    # transfusion — the patient is issued whatever compatible unit is oldest, not
    # the specific bag their cousin gave. `against_request_id` records which
    # requirement the donation was credited to; `is_directed` marks the rarer case
    # where this particular unit is reserved for that patient.
    against_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("blood_request.id"), nullable=True, index=True
    )
    donor_relationship: Mapped[str | None] = mapped_column(String(30), nullable=True)
    is_directed: Mapped[bool] = mapped_column(Boolean, default=False)

    collected_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), index=True
    )
    donation_type: Mapped[str] = mapped_column(String(20), default="WHOLE_BLOOD")
    bag_type: Mapped[str] = mapped_column(String(20), default="TRIPLE")
    anticoagulant: Mapped[str] = mapped_column(String(20), default="CPDA_1")
    volume_ml: Mapped[int] = mapped_column(Integer, default=450)

    # COLLECTED -> TESTED -> RELEASED -> PROCESSED, or QUARANTINED / DISCARDED
    status: Mapped[str] = mapped_column(String(20), default="COLLECTED", index=True)

    # Grouping is confirmed by the lab, not taken from the donor record.
    typed_blood_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("blood_group.id"), nullable=True, index=True
    )
    grouping_discrepancy: Mapped[bool] = mapped_column(Boolean, default=False)

    adverse_reaction: Mapped[str | None] = mapped_column(String(40), nullable=True)
    adverse_reaction_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    phlebotomist: Mapped[str | None] = mapped_column(String(120), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    released_by: Mapped[str | None] = mapped_column(String(120), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )


class LabRun(Base):
    """A batch of samples tested together: one plate, one kit lot, one operator.

    An ELISA lab does not test one sample at a time, it runs a plate of ninety or
    so. Modelling that is not cosmetic. The kit lot belongs to the RUN rather
    than to each result, and when a lot is recalled the question is "which
    donations did it touch" — one join away if the run exists, a manual search if
    it does not.

    The run is also where the controls live. A plate whose positive control did
    not react cannot be interpreted however clean the sample wells look, and that
    invalidity applies to every result on it at once.

    Named LabRun rather than TestRun because pytest collects classes called
    Test*, and a database model turning up as a broken test case is a confusing
    way to learn that.
    """

    __tablename__ = "lab_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    facility_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("facility.id"), index=True
    )

    # One marker per run, because that is what a plate is. A full panel is
    # several runs.
    test_code: Mapped[str] = mapped_column(String(30), index=True)
    test_group: Mapped[str] = mapped_column(String(20), default="TTI")
    method: Mapped[str | None] = mapped_column(String(40), nullable=True)

    kit_lot: Mapped[str | None] = mapped_column(String(60), index=True, nullable=True)
    kit_expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    equipment: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # OPEN -> RESULTS_ENTERED -> CLOSED, or INVALIDATED when controls fail.
    status: Mapped[str] = mapped_column(String(20), default="OPEN", index=True)

    controls_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    control_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    opened_at: Mapped[datetime] = mapped_column(UtcDateTime())
    opened_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    closed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class DonationTest(Base):
    """One test result against one donation.

    A row per test rather than a column per test, so adding a panel member is
    configuration rather than a migration. A single reactive row is enough to
    quarantine the donation, which is why the release check counts rows instead
    of reading a summary flag.
    """

    __tablename__ = "donation_test"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    donation_id: Mapped[str] = mapped_column(ForeignKey("donation.id"), index=True)

    # HIV, HBSAG, HCV, SYPHILIS, MALARIA, ABO_FORWARD, ABO_REVERSE, RHD,
    # ANTIBODY_SCREEN
    test_code: Mapped[str] = mapped_column(String(30), index=True)
    test_group: Mapped[str] = mapped_column(String(20), default="TTI")

    method: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Nullable on purpose: the generated history predates lab runs, and
    # inventing a run for it would assert a plate that never existed.
    lab_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("lab_run.id"), index=True, nullable=True
    )

    kit_lot: Mapped[str | None] = mapped_column(String(60), nullable=True)
    kit_expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    equipment: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # NON_REACTIVE / REACTIVE for TTI; a group code or Rh sign for typing.
    result: Mapped[str] = mapped_column(String(30))
    result_value: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_reactive: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    tested_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )
    tested_by: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Second-person verification before release, per standard practice.
    verified_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    verified_by: Mapped[str | None] = mapped_column(String(120), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # test_group is part of the key. A screening ELISA for HCV and its
        # confirmatory RIBA are two different tests of the same marker on the
        # same donation, and the confirmatory result is the one that decides
        # what happens to the donor — so it cannot be the screen's replacement.
        UniqueConstraint(
            "donation_id", "test_group", "test_code", name="uq_donation_test_code"
        ),
    )


class ComponentProduction(Base):
    """Separation of a released donation into components."""

    __tablename__ = "component_production"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    donation_id: Mapped[str] = mapped_column(ForeignKey("donation.id"), index=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)

    produced_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, index=True
    )
    method: Mapped[str] = mapped_column(String(40), default="CENTRIFUGE_HARD_SPIN")
    recipe_code: Mapped[str | None] = mapped_column(String(40), nullable=True)

    units_expected: Mapped[int] = mapped_column(Integer, default=0)
    units_produced: Mapped[int] = mapped_column(Integer, default=0)

    # What the recipe called for, and what was actually made. Stored as codes
    # rather than inferred from the units, because the difference between them
    # IS the record — a bag that was meant to yield a platelet and did not is
    # invisible if you only look at what exists.
    expected_components: Mapped[list | None] = mapped_column(JSON, nullable=True)
    produced_components: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Why each expected component was not made: {"PLT_RD": "FAILED_SPIN"}. The
    # number a bank tries to reduce, and it cannot be reduced if it is not
    # attributed.
    loss_reasons: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Hours from the needle to the spin. Platelets must come off inside eight;
    # recording the interval means a facility can see how close it runs.
    minutes_from_collection: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    produced_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class StorageLocation(Base):
    """A fridge, freezer, platelet agitator or quarantine shelf."""

    __tablename__ = "storage_location"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)

    code: Mapped[str] = mapped_column(String(30), index=True)
    name: Mapped[str] = mapped_column(String(120))
    location_type: Mapped[str] = mapped_column(String(30), default="FRIDGE")

    target_temp_min_c: Mapped[float] = mapped_column(Float, default=2.0)
    target_temp_max_c: Mapped[float] = mapped_column(Float, default=6.0)
    capacity_units: Mapped[int | None] = mapped_column(Integer, nullable=True)

    is_quarantine: Mapped[bool] = mapped_column(Boolean, default=False)
    has_agitator: Mapped[bool] = mapped_column(Boolean, default=False)

    last_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_temp_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    is_out_of_range: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("facility_id", "code", name="uq_storage_location_code"),
    )


class TemperatureLog(Base):
    """Manual or device temperature reading against a storage location.

    Real IoT telemetry is out of scope for the MVP (spec §14.2), but a
    twice-daily manual log is standard practice and is what an inspector asks to
    see.
    """

    __tablename__ = "temperature_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    storage_location_id: Mapped[str] = mapped_column(
        ForeignKey("storage_location.id"), index=True
    )

    recorded_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, index=True
    )
    temperature_c: Mapped[float] = mapped_column(Float)
    is_out_of_range: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    source: Mapped[str] = mapped_column(String(20), default="MANUAL")
    recorded_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    action_taken: Mapped[str | None] = mapped_column(Text, nullable=True)


class BloodRequest(Base):
    """A clinician's request for blood.

    The patient is referenced pseudonymously. Spec §13.2 forbids patient
    identifiers reaching the decision layer, and a blood bank does not need a
    name to issue a unit — it needs a stable episode reference, a group and a
    ward. `patient_ref` is that reference, supplied by the hospital's own system.
    """

    __tablename__ = "blood_request"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    request_code: Mapped[str] = mapped_column(String(30), unique=True, index=True)

    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)

    patient_ref: Mapped[str] = mapped_column(String(60), index=True)
    patient_age_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    patient_sex: Mapped[str | None] = mapped_column(String(10), nullable=True)
    patient_blood_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("blood_group.id"), nullable=True, index=True
    )

    component_id: Mapped[int] = mapped_column(ForeignKey("component.id"), index=True)
    units_requested: Mapped[int] = mapped_column(Integer, default=1)
    units_issued: Mapped[int] = mapped_column(Integer, default=0)

    urgency: Mapped[str] = mapped_column(String(30), default="ROUTINE", index=True)
    clinical_context: Mapped[str] = mapped_column(String(40), default="OTHER")

    # Replacement policy for this requirement. Most Pakistani blood banks ask the
    # family to replace what the patient consumes, so the ledger a clerk needs is
    # "4 required, 3 replaced, 1 outstanding". Zero required means a fully
    # voluntary-supplied episode, which is the direction the national policy
    # pushes towards and which the network layer makes easier.
    replacement_units_required: Mapped[int] = mapped_column(Integer, default=0)
    replacement_units_received: Mapped[int] = mapped_column(Integer, default=0)
    replacement_waived: Mapped[bool] = mapped_column(Boolean, default=False)
    replacement_waived_reason: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )

    ward: Mapped[str | None] = mapped_column(String(120), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    consultant: Mapped[str | None] = mapped_column(String(160), nullable=True)

    requested_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, index=True
    )
    required_by: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )

    # PENDING -> CROSSMATCHED -> ISSUED / PARTIAL -> CLOSED, or CANCELLED
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)

    # Set when a compatible non-identical group was supplied.
    was_substituted: Mapped[bool] = mapped_column(Boolean, default=False)
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    override_by: Mapped[str | None] = mapped_column(String(120), nullable=True)

    closed_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Crossmatch(Base):
    """Compatibility testing of one unit against one request."""

    __tablename__ = "crossmatch"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    request_id: Mapped[str] = mapped_column(ForeignKey("blood_request.id"), index=True)
    blood_unit_id: Mapped[str] = mapped_column(ForeignKey("blood_unit.id"), index=True)

    method: Mapped[str] = mapped_column(String(30), default="AHG_COOMBS")
    result: Mapped[str] = mapped_column(String(20), default="COMPATIBLE", index=True)

    performed_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )
    performed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # A crossmatch is time-limited; an expired one must be repeated.
    valid_until: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "request_id", "blood_unit_id", name="uq_crossmatch_request_unit"
        ),
    )


class UnitIssue(Base):
    """Handover of a unit out of the blood bank."""

    __tablename__ = "unit_issue"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    blood_unit_id: Mapped[str] = mapped_column(ForeignKey("blood_unit.id"), index=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("blood_request.id"), index=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)

    issued_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, index=True
    )
    issued_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    collected_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    destination_ward: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # CROSSMATCHED for the standard workflow, EMERGENCY_UNCROSSMATCHED only for
    # an explicitly authorized emergency release. Keeping the release basis on
    # the issue row makes the exception visible in traceability and reports;
    # it must not be reconstructed later from the absence of a crossmatch.
    release_mode: Mapped[str] = mapped_column(
        String(30), default="CROSSMATCHED", index=True
    )
    emergency_release_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    emergency_authorized_by: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )

    # A custody handover is unresolved until one of these terminal outcomes is
    # recorded: TRANSFUSED, RETURNED_TO_STOCK, RETURN_REJECTED, NOT_RETURNED.
    disposition: Mapped[str] = mapped_column(
        String(30), default="AWAITING_OUTCOME", index=True
    )
    custody_closed_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True, index=True
    )
    custody_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # A unit can come back. Whether it may be re-shelved depends on how long it
    # was out and whether the cold chain held.
    returned_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    return_accepted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    return_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)
    minutes_out_of_storage: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TransfusionRecord(Base):
    """The other end of vein to vein: what happened to the patient.

    Without this the chain stops at issue, and an issued unit is not the same as
    a transfused one — the difference is the return and wastage story.
    """

    __tablename__ = "transfusion_record"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    blood_unit_id: Mapped[str] = mapped_column(ForeignKey("blood_unit.id"), index=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("blood_request.id"), index=True)
    issue_id: Mapped[str | None] = mapped_column(
        ForeignKey("unit_issue.id"), nullable=True
    )

    started_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True, index=True
    )

    outcome: Mapped[str] = mapped_column(String(20), default="COMPLETED", index=True)

    # NONE, FEBRILE_NON_HAEMOLYTIC, ALLERGIC, ANAPHYLACTIC, ACUTE_HAEMOLYTIC,
    # DELAYED_HAEMOLYTIC, TACO, TRALI, BACTERIAL_CONTAMINATION, OTHER
    reaction_type: Mapped[str] = mapped_column(String(40), default="NONE", index=True)
    reaction_severity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reaction_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reaction_reported_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )

    recorded_by: Mapped[str | None] = mapped_column(String(120), nullable=True)


class DonationBatch(Base):
    __tablename__ = "donation_batch"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    facility_id: Mapped[str] = mapped_column(
        ForeignKey("facility.id"), index=True
    )
    collected_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    source: Mapped[str] = mapped_column(String(30), default="ON_SITE")
    blood_group_id: Mapped[int] = mapped_column(
        ForeignKey("blood_group.id"), index=True
    )
    component_id: Mapped[int] = mapped_column(
        ForeignKey("component.id"), index=True
    )
    units_collected: Mapped[int] = mapped_column(Integer, default=1)
    donor_count: Mapped[int] = mapped_column(Integer, default=1)


class BloodUnit(Base):
    __tablename__ = "blood_unit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    din: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("component.id"), index=True)
    blood_group_id: Mapped[int] = mapped_column(ForeignKey("blood_group.id"), index=True)

    volume_ml: Mapped[int] = mapped_column(Integer, default=350)
    collected_at: Mapped[datetime] = mapped_column(UtcDateTime())
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)

    status: Mapped[str] = mapped_column(String(30), default="AVAILABLE", index=True)
    screening_status: Mapped[str] = mapped_column(String(20), default="PASSED")

    is_leucodepleted: Mapped[bool] = mapped_column(Boolean, default=False)
    is_irradiated: Mapped[bool] = mapped_column(Boolean, default=False)
    cold_chain_breach_count: Mapped[int] = mapped_column(Integer, default=0)

    isbt_product_code: Mapped[str | None] = mapped_column(String(8), nullable=True)

    # The parent donation, and through it the donor. This is what makes the chain
    # of custody answerable in both directions: from a bag back to the person who
    # gave it, and from a reactive test forward to every unit that must be pulled.
    donation_id: Mapped[str | None] = mapped_column(
        ForeignKey("donation.id"), nullable=True, index=True
    )
    storage_location_id: Mapped[str | None] = mapped_column(
        ForeignKey("storage_location.id"), nullable=True, index=True
    )

    # Idempotency key from the origin BBMIS (spec §4.2). Adapters upsert on
    # (facility_id, source_system_ref).
    source_system_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )

    # Terminal-state history. Without these, wastage rate, expiry share of
    # wastage and days-to-expiry-at-issue cannot be measured at all, which is
    # what the whole impact case rests on (spec §15.4, §12.10).
    issued_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True, index=True
    )
    discarded_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True, index=True
    )
    discard_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "facility_id",
            "source_system_ref",
            name="uq_blood_unit_facility_source_ref",
        ),
    )


class DemandEvent(Base):
    __tablename__ = "demand_event"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # A locally-created clinical request is canonical demand too.  Imported
    # events legitimately have no request id; the nullable, unique link keeps
    # the clinical workflow idempotent without changing external adapter keys.
    blood_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("blood_request.id"), nullable=True, unique=True, index=True
    )
    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("component.id"), index=True)
    blood_group_id: Mapped[int] = mapped_column(ForeignKey("blood_group.id"), index=True)

    requested_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    units_requested: Mapped[int] = mapped_column(Integer, default=0)
    units_issued: Mapped[int] = mapped_column(Integer, default=0)

    urgency: Mapped[str] = mapped_column(String(30), default="ROUTINE")
    clinical_context: Mapped[str] = mapped_column(String(40), default="OTHER")
    was_substituted: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str] = mapped_column(String(30), default="FULFILLED")

    # Canonical idempotency contract shared by manual CSV, FHIR, HL7 and the
    # simulated adapter. Historic generated rows legitimately have no source
    # reference; imported records must always carry one.
    source_system_ref: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )

    __table_args__ = (
        Index(
            "uq_demand_event_facility_source_ref",
            "facility_id",
            "source_system_ref",
            unique=True,
        ),
    )


class TransferPlan(Base):
    __tablename__ = "transfer_plan"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    plan_type: Mapped[str] = mapped_column(String(30), default="ROUTINE")
    status: Mapped[str] = mapped_column(String(30), default="GENERATED")
    scope: Mapped[str] = mapped_column(String(30), default="PROVINCE")
    parameters_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(120), default="system")


class Transfer(Base):
    __tablename__ = "transfer"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(ForeignKey("transfer_plan.id"), index=True)

    from_facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)
    to_facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("component.id"), index=True)

    # The group of the units being shipped.
    blood_group_id: Mapped[int] = mapped_column(ForeignKey("blood_group.id"), index=True)

    # The group whose projected demand this shipment answers. Differs from
    # blood_group_id whenever a compatible substitute is being sent, and is what
    # makes the compatibility path auditable after the fact.
    recipient_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("blood_group.id"), nullable=True, index=True
    )
    preference_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    units: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="RECOMMENDED")

    # Specific bags chosen FEFO after the solve (spec §8.1). Stored as JSON so
    # the same model works on SQLite and PostgreSQL.
    unit_ids: Mapped[list] = mapped_column(JSON, default=list)

    est_travel_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    distance_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    transport_mode: Mapped[str | None] = mapped_column(String(30), nullable=True)

    rationale_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale_ur: Mapped[str | None] = mapped_column(Text, nullable=True)

    projected_units_saved: Mapped[float | None] = mapped_column(Float, nullable=True)
    projected_shortage_averted: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    recommended_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    approved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    modified_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    modified_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )

    # Dispatch custody. A transfer is not merely a status change: these fields
    # are the hand-off record an inspector follows from the releasing blood bank
    # to the receiving store. `tracking_code` is deliberately non-clinical and
    # safe to put on a courier label or QR code.
    tracking_code: Mapped[str | None] = mapped_column(
        String(30), nullable=True, unique=True, index=True
    )
    dispatched_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    dispatch_custodian: Mapped[str | None] = mapped_column(String(120), nullable=True)
    courier_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    courier_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    vehicle_ref: Mapped[str | None] = mapped_column(String(60), nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    seal_number: Mapped[str | None] = mapped_column(String(60), nullable=True)
    departure_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)

    in_transit_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    in_transit_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )

    # Receipt is recorded at unit grain. A partial delivery must never silently
    # move the whole manifest to the destination, and a temperature exception
    # must quarantine what arrived rather than returning it to usable stock.
    received_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    receiving_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    seal_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    receipt_disposition: Mapped[str | None] = mapped_column(String(30), nullable=True)
    received_unit_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    accepted_unit_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    quarantined_unit_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    missing_unit_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    discrepancy_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    discrepancy_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)

    cancelled_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Structured rejection reasons are training data for weight tuning
    # (spec §8.4), not just an audit note.
    rejection_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)
    rejection_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Forecast(Base):
    __tablename__ = "forecast"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36), index=True)

    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("component.id"), index=True)
    blood_group_id: Mapped[int] = mapped_column(ForeignKey("blood_group.id"), index=True)

    target_date: Mapped[date] = mapped_column(Date, index=True)
    horizon_days: Mapped[int] = mapped_column(Integer, default=1)

    p10: Mapped[float] = mapped_column(Float, default=0.0)
    p50: Mapped[float] = mapped_column(Float, default=0.0)
    p90: Mapped[float] = mapped_column(Float, default=0.0)

    model_version: Mapped[str] = mapped_column(String(80), default="baseline-v1")
    generated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class Alert(Base):
    __tablename__ = "alert"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    facility_id: Mapped[str | None] = mapped_column(
        ForeignKey("facility.id"), nullable=True, index=True
    )
    organization_id: Mapped[str | None] = mapped_column(
        ForeignKey("organization.id"), nullable=True, index=True
    )
    component_id: Mapped[int | None] = mapped_column(
        ForeignKey("component.id"), nullable=True, index=True
    )
    blood_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("blood_group.id"), nullable=True, index=True
    )

    alert_type: Mapped[str] = mapped_column(String(50), index=True)
    severity: Mapped[str] = mapped_column(String(20), default="MEDIUM")
    status: Mapped[str] = mapped_column(String(20), default="OPEN")

    title_en: Mapped[str] = mapped_column(String(255))
    title_ur: Mapped[str | None] = mapped_column(String(255), nullable=True)

    body_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_ur: Mapped[str | None] = mapped_column(Text, nullable=True)

    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    source_entity_type: Mapped[str | None] = mapped_column(
        String(80), nullable=True
    )
    source_entity_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )

    # Deduplication key: one open alert per (facility, type, component, group);
    # new evidence updates the existing alert rather than raising another
    # (spec §11.2). Alert fatigue is how systems like this die in month three.
    dedup_key: Mapped[str] = mapped_column(String(200), index=True, default="")
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    last_notified_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    acknowledged_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    acknowledgement_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_to: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    escalated_to: Mapped[str | None] = mapped_column(String(120), nullable=True)


class AlertDelivery(Base):
    """Durable notification outbox; no external send is implied by an alert."""

    __tablename__ = "alert_delivery"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    alert_id: Mapped[str] = mapped_column(ForeignKey("alert.id"), index=True)
    channel: Mapped[str] = mapped_column(String(20), default="IN_APP")
    recipient: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    suppressed_reason: Mapped[str | None] = mapped_column(
        String(80), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow, index=True)

    actor: Mapped[str] = mapped_column(String(120), default="system")
    action: Mapped[str] = mapped_column(String(120))
    entity_type: Mapped[str] = mapped_column(String(120))
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    actor_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)


class AiInteraction(Base):
    """Privacy-minimised evidence for every Qwen attempt and safe fallback.

    Raw prompts and user questions are deliberately absent. The source hash
    supports tenant-scoped caching and reproducibility without copying donor or
    patient data into a second store; ``result_json`` contains only validated,
    presentation-safe output.
    """

    __tablename__ = "ai_interaction"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, index=True
    )
    feature: Mapped[str] = mapped_column(String(50), index=True)
    language: Mapped[str] = mapped_column(String(5), default="en")
    status: Mapped[str] = mapped_column(String(30), index=True)
    provider: Mapped[str] = mapped_column(String(30), default="qwen")
    model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(40))

    organization_id: Mapped[str | None] = mapped_column(
        ForeignKey("organization.id"), nullable=True, index=True
    )
    facility_id: Mapped[str | None] = mapped_column(
        ForeignKey("facility.id"), nullable=True, index=True
    )
    actor_user_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_role: Mapped[str] = mapped_column(String(40))
    scope_json: Mapped[list] = mapped_column(JSON, default=list)

    source_hash: Mapped[str] = mapped_column(String(64), index=True)
    question_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    request_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )

    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_chars: Mapped[int] = mapped_column(Integer, default=0)
    output_chars: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)

    validation_json: Mapped[dict] = mapped_column(JSON, default=dict)
    fallback_reason: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)


class PlatformSetting(Base):
    """Audited runtime overrides for administrator-tunable engine settings."""

    __tablename__ = "platform_setting"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str] = mapped_column(String(160))
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class IntelligenceRefreshState(Base):
    """Durable transaction-to-intelligence refresh state.

    Operational writes increment ``source_version`` in the same transaction as
    their audit event.  The background refresh records which version it
    completed, so a write that arrives while a refresh is running cannot be
    accidentally declared clean.
    """

    __tablename__ = "intelligence_refresh_state"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="DIRTY", index=True)
    source_version: Mapped[int] = mapped_column(Integer, default=0)
    completed_version: Mapped[int] = mapped_column(Integer, default=0)

    reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    failed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class IntegrationFeed(Base):
    """One adapter connection and its operational health per facility.

    This is deliberately separate from analytical marts. Feed state is an
    operational fact: an adapter can fail even while yesterday's aggregates
    remain queryable, and the UI must say so instead of silently presenting
    stale figures as current.
    """

    __tablename__ = "integration_feed"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id"), index=True
    )
    facility_id: Mapped[str] = mapped_column(
        ForeignKey("facility.id"), unique=True, index=True
    )
    mode: Mapped[str] = mapped_column(String(30), default="SIMULATED")
    status: Mapped[str] = mapped_column(String(20), default="NEVER_SYNCED", index=True)
    capabilities_json: Mapped[dict] = mapped_column(JSON, default=dict)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    cursor_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    rows_seen: Mapped[int] = mapped_column(Integer, default=0)
    rows_ingested: Mapped[int] = mapped_column(Integer, default=0)
    rows_quarantined: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class ImportBatch(Base):
    """Immutable source envelope plus the mutable preview/commit lifecycle."""

    __tablename__ = "import_batch"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id"), index=True
    )
    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)
    feed_id: Mapped[str | None] = mapped_column(
        ForeignKey("integration_feed.id"), nullable=True, index=True
    )
    mode: Mapped[str] = mapped_column(String(30), default="MANUAL", index=True)
    data_type: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(24), default="PREVIEW", index=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    checksum_sha256: Mapped[str] = mapped_column(String(64), index=True)
    payload_bytes: Mapped[int] = mapped_column(Integer, default=0)
    source_headers_json: Mapped[list] = mapped_column(JSON, default=list)
    field_mapping_json: Mapped[dict] = mapped_column(JSON, default=dict)
    validation_summary_json: Mapped[dict] = mapped_column(JSON, default=dict)
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    valid_rows: Mapped[int] = mapped_column(Integer, default=0)
    quarantined_rows: Mapped[int] = mapped_column(Integer, default=0)
    rejected_rows: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, default=0)
    ingested_rows: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    committed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    committed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "facility_id",
            "mode",
            "data_type",
            "checksum_sha256",
            name="uq_import_batch_source_payload",
        ),
    )


class ImportRow(Base):
    """A source row, its canonical form, and every validation decision."""

    __tablename__ = "import_row"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batch.id"), index=True)
    row_number: Mapped[int] = mapped_column(Integer)
    source_system_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    raw_json: Mapped[dict] = mapped_column(JSON, default=dict)
    normalized_json: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="VALID", index=True)
    errors_json: Mapped[list] = mapped_column(JSON, default=list)
    warnings_json: Mapped[list] = mapped_column(JSON, default=list)
    entity_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    ingested_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)

    __table_args__ = (
        UniqueConstraint("batch_id", "row_number", name="uq_import_row_number"),
    )


class IntegrationArchive(Base):
    """Raw source payload retained before parsing for replay and evidence."""

    __tablename__ = "integration_archive"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("import_batch.id"), unique=True, index=True
    )
    checksum_sha256: Mapped[str] = mapped_column(String(64), index=True)
    content_type: Mapped[str] = mapped_column(String(120), default="text/csv")
    payload_text: Mapped[str] = mapped_column(Text)
    payload_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class SourceProvenance(Base):
    """Stable link from a local domain row back to its originating record."""

    __tablename__ = "source_provenance"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id"), index=True
    )
    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batch.id"), index=True)
    source_mode: Mapped[str] = mapped_column(String(30), index=True)
    source_system_ref: Mapped[str] = mapped_column(String(120), index=True)
    entity_type: Mapped[str] = mapped_column(String(60), index=True)
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    version_count: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        UniqueConstraint(
            "facility_id",
            "source_mode",
            "source_system_ref",
            "entity_type",
            name="uq_provenance_source_entity",
        ),
    )


class ReconciliationIssue(Base):
    """A mismatch that must remain visible until somebody resolves it."""

    __tablename__ = "reconciliation_issue"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id"), index=True
    )
    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batch.id"), index=True)
    import_row_id: Mapped[str | None] = mapped_column(
        ForeignKey("import_row.id"), nullable=True, index=True
    )
    issue_type: Mapped[str] = mapped_column(String(60), index=True)
    severity: Mapped[str] = mapped_column(String(20), default="WARNING")
    status: Mapped[str] = mapped_column(String(20), default="OPEN", index=True)
    summary: Mapped[str] = mapped_column(String(255))
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    resolved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApiClient(Base):
    """Hashed machine credential scoped to one tenant and explicit capabilities."""

    __tablename__ = "api_client"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    key_prefix: Mapped[str] = mapped_column(String(20), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scopes_json: Mapped[list] = mapped_column(JSON, default=list)
    facility_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=120)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class InventorySnapshot(Base):
    """Daily aggregate position (spec §4.2).

    Two jobs: it is the only feed shape some facilities can send, and it is the
    long-history substitute for unit-level rows, which are too numerous to keep
    at unit grain for eighteen months.
    """

    __tablename__ = "inventory_snapshot"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("component.id"), index=True)
    blood_group_id: Mapped[int] = mapped_column(
        ForeignKey("blood_group.id"), index=True
    )

    units_available: Mapped[int] = mapped_column(Integer, default=0)
    units_reserved: Mapped[int] = mapped_column(Integer, default=0)
    units_expiring_7d: Mapped[int] = mapped_column(Integer, default=0)
    units_expiring_3d: Mapped[int] = mapped_column(Integer, default=0)

    units_issued: Mapped[int] = mapped_column(Integer, default=0)
    units_expired: Mapped[int] = mapped_column(Integer, default=0)
    units_discarded: Mapped[int] = mapped_column(Integer, default=0)
    units_collected: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint(
            "snapshot_date",
            "facility_id",
            "component_id",
            "blood_group_id",
            name="uq_inventory_snapshot_series_date",
        ),
    )


class ForecastMetric(Base):
    """Backtest results per series (spec §6.4, §12.10 "Forecast quality").

    Acceptance criteria 3 and 4 are numeric claims about model quality. They
    need a stored measurement, and the UI has to be able to show the model
    beaten by its own baseline without the developer choosing to mention it.
    """

    __tablename__ = "forecast_metric"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36), index=True)

    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("component.id"), index=True)
    blood_group_id: Mapped[int] = mapped_column(
        ForeignKey("blood_group.id"), index=True
    )

    horizon_days: Mapped[int] = mapped_column(Integer, default=7)
    regime: Mapped[str] = mapped_column(String(20), default="UNKNOWN")
    model_version: Mapped[str] = mapped_column(String(80), default="baseline-v1")

    folds: Mapped[int] = mapped_column(Integer, default=0)
    n_observations: Mapped[int] = mapped_column(Integer, default=0)
    actual_total: Mapped[float] = mapped_column(Float, default=0.0)

    wape: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Estimated irreducible error for this series. WAPE without it is
    # uninterpretable: 45% error against a 44% noise floor is a good model, and
    # 20% error against a 5% floor is a poor one.
    wape_noise_floor: Mapped[float | None] = mapped_column(Float, nullable=True)

    mae: Mapped[float | None] = mapped_column(Float, nullable=True)
    mase: Mapped[float | None] = mapped_column(Float, nullable=True)
    pinball_p10: Mapped[float | None] = mapped_column(Float, nullable=True)
    pinball_p90: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_pinball_p90: Mapped[float | None] = mapped_column(Float, nullable=True)
    picp: Mapped[float | None] = mapped_column(Float, nullable=True)

    baseline_seasonal_naive_wape: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    baseline_trailing_mean_wape: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    beats_baselines: Mapped[bool] = mapped_column(Boolean, default=False)

    # Spec §6.3: if a model does not beat both baselines on backtest, the system
    # falls back to the baseline and says so. Displaying this honestly is a
    # credibility asset, not a weakness.
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False)

    generated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "facility_id",
            "component_id",
            "blood_group_id",
            "horizon_days",
            name="uq_forecast_metric_series_horizon",
        ),
    )


class ForecastRunSummary(Base):
    """Network-level backtest headline (spec §6.4, acceptance criteria 3 and 4)."""

    __tablename__ = "forecast_run_summary"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36), index=True, unique=True)

    generated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )

    series_total: Mapped[int] = mapped_column(Integer, default=0)
    series_dense: Mapped[int] = mapped_column(Integer, default=0)
    series_fallback: Mapped[int] = mapped_column(Integer, default=0)

    wape_dense_7d: Mapped[float | None] = mapped_column(Float, nullable=True)
    wape_all_7d: Mapped[float | None] = mapped_column(Float, nullable=True)
    noise_floor_dense_7d: Mapped[float | None] = mapped_column(Float, nullable=True)
    pct_series_beating_naive: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    picp_p10_p90: Mapped[float | None] = mapped_column(Float, nullable=True)
    shortage_detection_recall_3d: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )

    metrics_json: Mapped[dict] = mapped_column(JSON, default=dict)


class MartDailyDemand(Base):
    """Gap-filled daily demand series (spec §4.3).

    Zeros are explicit. A chart built from raw demand_event rows silently omits
    the days a rare group was not requested, which makes an intermittent series
    look like a dense one.
    """

    __tablename__ = "mart_daily_demand"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("component.id"), index=True)
    blood_group_id: Mapped[int] = mapped_column(
        ForeignKey("blood_group.id"), index=True
    )
    demand_date: Mapped[date] = mapped_column(Date, index=True)

    units_requested: Mapped[int] = mapped_column(Integer, default=0)
    units_issued: Mapped[int] = mapped_column(Integer, default=0)
    units_unmet: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint(
            "facility_id",
            "component_id",
            "blood_group_id",
            "demand_date",
            name="uq_mart_daily_demand_series_date",
        ),
    )


class MartDaysOfCover(Base):
    """Current stock position per series, in days of cover.

    Backs the Command Centre heatmap (spec §12.4), which is the one view a blood
    bank officer reads first, so it must not require a scan of the unit table.
    """

    __tablename__ = "mart_days_of_cover"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    facility_id: Mapped[str] = mapped_column(ForeignKey("facility.id"), index=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("component.id"), index=True)
    blood_group_id: Mapped[int] = mapped_column(
        ForeignKey("blood_group.id"), index=True
    )

    units_available: Mapped[int] = mapped_column(Integer, default=0)
    units_reserved: Mapped[int] = mapped_column(Integer, default=0)
    units_expiring_72h: Mapped[int] = mapped_column(Integer, default=0)
    units_expiring_7d: Mapped[int] = mapped_column(Integer, default=0)

    avg_daily_demand: Mapped[float] = mapped_column(Float, default=0.0)
    days_of_cover: Mapped[float | None] = mapped_column(Float, nullable=True)
    reserve_floor: Mapped[float] = mapped_column(Float, default=0.0)

    shortage_probability: Mapped[float] = mapped_column(Float, default=0.0)
    risk_bucket: Mapped[str] = mapped_column(String(20), default="SAFE")

    generated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "facility_id",
            "component_id",
            "blood_group_id",
            name="uq_mart_days_of_cover_series",
        ),
    )


class MartFacilityKpi(Base):
    """One row per facility: everything the map, list and tiles need."""

    __tablename__ = "mart_facility_kpi"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    facility_id: Mapped[str] = mapped_column(
        ForeignKey("facility.id"), index=True, unique=True
    )
    facility_code: Mapped[str] = mapped_column(String(30))
    name_en: Mapped[str] = mapped_column(String(255))
    name_ur: Mapped[str | None] = mapped_column(String(255), nullable=True)
    facility_type: Mapped[str] = mapped_column(String(30))
    district: Mapped[str] = mapped_column(String(120))
    division: Mapped[str | None] = mapped_column(String(120), nullable=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    parent_rbc_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    units_available: Mapped[int] = mapped_column(Integer, default=0)
    min_days_of_cover: Mapped[float | None] = mapped_column(Float, nullable=True)
    worst_component_code: Mapped[str | None] = mapped_column(String(12), nullable=True)
    worst_group_code: Mapped[str | None] = mapped_column(String(3), nullable=True)

    critical_series: Mapped[int] = mapped_column(Integer, default=0)
    warning_series: Mapped[int] = mapped_column(Integer, default=0)

    units_at_risk: Mapped[int] = mapped_column(Integer, default=0)
    units_unrescuable: Mapped[int] = mapped_column(Integer, default=0)

    wastage_pct_30d: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_rate_30d: Mapped[float | None] = mapped_column(Float, nullable=True)

    transfers_in_pending: Mapped[int] = mapped_column(Integer, default=0)
    transfers_out_pending: Mapped[int] = mapped_column(Integer, default=0)

    # Spec §5.8 degradation principle: a facility with a stale feed is visibly
    # marked, never silently dropped. Acceptance criterion 12.
    integration_mode: Mapped[str] = mapped_column(String(30), default="SIMULATED")
    last_synced_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    feed_status: Mapped[str] = mapped_column(String(20), default="HEALTHY")
    feed_age_hours: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Positive means surplus relative to requirement, negative means deficit.
    balance_index: Mapped[float] = mapped_column(Float, default=0.0)

    generated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )


class MartImpact(Base):
    """Daily network-level flow, for the impact and wastage trends (spec §12.10)."""

    __tablename__ = "mart_impact"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    impact_date: Mapped[date] = mapped_column(Date, index=True, unique=True)

    units_collected: Mapped[int] = mapped_column(Integer, default=0)
    units_issued: Mapped[int] = mapped_column(Integer, default=0)
    units_expired: Mapped[int] = mapped_column(Integer, default=0)
    units_discarded: Mapped[int] = mapped_column(Integer, default=0)

    units_requested: Mapped[int] = mapped_column(Integer, default=0)
    units_unmet: Mapped[int] = mapped_column(Integer, default=0)

    wastage_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_rate: Mapped[float | None] = mapped_column(Float, nullable=True)


class ShortageRisk(Base):
    __tablename__ = "shortage_risk"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    facility_id: Mapped[str] = mapped_column(
        ForeignKey("facility.id"), index=True
    )
    component_id: Mapped[int] = mapped_column(
        ForeignKey("component.id"), index=True
    )
    blood_group_id: Mapped[int] = mapped_column(
        ForeignKey("blood_group.id"), index=True
    )

    risk_date: Mapped[date] = mapped_column(Date, index=True)
    horizon_days: Mapped[int] = mapped_column(Integer, default=1)

    on_hand_base: Mapped[float] = mapped_column(Float, default=0.0)
    projected_available: Mapped[float] = mapped_column(Float, default=0.0)
    required_p50: Mapped[float] = mapped_column(Float, default=0.0)
    required_p90: Mapped[float] = mapped_column(Float, default=0.0)
    reserve_floor: Mapped[float] = mapped_column(Float, default=0.0)

    shortage_probability: Mapped[float] = mapped_column(Float, default=0.0)
    risk_bucket: Mapped[str] = mapped_column(String(20), default="SAFE")

    generated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "facility_id",
            "component_id",
            "blood_group_id",
            "risk_date",
            name="uq_shortage_risk_series_date",
        ),
    )


class ExpiryRescue(Base):
    __tablename__ = "expiry_rescue"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36), index=True)

    blood_unit_id: Mapped[str] = mapped_column(
        ForeignKey("blood_unit.id"), index=True
    )

    facility_id: Mapped[str] = mapped_column(
        ForeignKey("facility.id"), index=True
    )
    component_id: Mapped[int] = mapped_column(
        ForeignKey("component.id"), index=True
    )
    blood_group_id: Mapped[int] = mapped_column(
        ForeignKey("blood_group.id"), index=True
    )

    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    days_left: Mapped[float] = mapped_column(Float, default=0.0)

    waste_probability: Mapped[float] = mapped_column(Float, default=0.0)
    rescue_tier: Mapped[str] = mapped_column(String(20), default="WATCH")
    transferable: Mapped[bool] = mapped_column(Boolean, default=False)

    best_recipient_facility_id: Mapped[str | None] = mapped_column(
        ForeignKey("facility.id"), nullable=True
    )
    best_travel_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Latest moment a dispatch can still leave and arrive usable:
    # expires_at - travel time - handling buffer. Waste probability alone does
    # not order the work queue — a unit ten days out can carry a high probability
    # of being wasted while nothing needs to happen about it today.
    dispatch_deadline_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    hours_to_deadline: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_need_score: Mapped[float] = mapped_column(Float, default=0.0)
    rescue_value: Mapped[float] = mapped_column(Float, default=0.0)

    reason_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_ur: Mapped[str | None] = mapped_column(Text, nullable=True)

    generated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )

class SimulationRun(Base):
    __tablename__ = "simulation_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow
    )
    organization_id: Mapped[str | None] = mapped_column(
        ForeignKey("organization.id"), nullable=True, index=True
    )
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    mode: Mapped[str] = mapped_column(String(30), default="PREPAREDNESS")
    parent_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("simulation_run.id"), nullable=True, index=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    name: Mapped[str] = mapped_column(String(255), default="Simulation")
    event_type: Mapped[str] = mapped_column(String(50), default="CUSTOM")

    seed: Mapped[int] = mapped_column(Integer, default=42)
    iterations: Mapped[int] = mapped_column(Integer, default=1000)

    scenario_json: Mapped[dict] = mapped_column(JSON, default=dict)
    results_json: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(String(20), default="COMPLETED")


class EmergencyIncident(Base):
    """Explicit live declaration created from, but never confused with, a drill."""

    __tablename__ = "emergency_incident"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    simulation_run_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_run.id"), unique=True, index=True
    )
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id"), index=True
    )
    transfer_plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("transfer_plan.id"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    declared_by: Mapped[str] = mapped_column(String(120))
    declared_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    resolved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
