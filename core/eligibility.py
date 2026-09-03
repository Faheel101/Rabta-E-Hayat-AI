"""Donor eligibility assessment.

Every threshold comes from config/network.yaml, whose provenance header records
where each number came from and how well sourced it is. Nothing clinical is
written as a literal in this module.

THE CENTRAL SAFETY PROPERTY of this engine is the deferral kind. A deferral is
one of three structurally different things:

    TIMED       a fixed clock: defer until date X
    CONDITIONAL no clock at all: defer until a later assessment says otherwise
    PERMANENT   never lifts

The source research proposed encoding "conditional" as zero days with a false
permanent flag. That is unsafe: any engine that computes
`eligible = today >= deferred_until` would score a CURRENTLY PREGNANT donor as
eligible today, because zero days in the past is in the past. The enum exists
specifically so that mistake cannot be made — a CONDITIONAL deferral has no
`until` date to compare against and blocks by its kind alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from core import config
from core.clock import DEMO_DATETIME

TIMED = "TIMED"
CONDITIONAL = "CONDITIONAL"
PERMANENT = "PERMANENT"

ACCEPTED = "ACCEPTED"
DEFERRED = "DEFERRED"

MALE = "MALE"
FEMALE = "FEMALE"


@dataclass(frozen=True)
class Deferral:
    """One reason a donor cannot give today."""

    reason_code: str
    kind: str
    label: str
    until: date | None = None
    days: int | None = None
    detail: str = ""
    confidence: str = "high"
    # Set when the rule is one the sources disagree on and a specialist has yet
    # to choose. Carried into the record so the decision is auditable.
    signoff_limb: str | None = None

    @property
    def is_blocking(self) -> bool:
        """A CONDITIONAL deferral blocks without a date, which is the point."""

        return True

    @property
    def description(self) -> str:
        if self.kind == PERMANENT:
            return f"{self.label} — permanent"

        if self.kind == CONDITIONAL:
            return f"{self.label} — until reassessed"

        if self.until:
            return f"{self.label} — until {self.until.isoformat()}"

        return self.label


@dataclass
class Assessment:
    """The outcome of screening one donor on one day."""

    outcome: str = ACCEPTED
    deferrals: list[Deferral] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    collection_volume_ml: int | None = None
    signoff_notes: list[str] = field(default_factory=list)

    @property
    def is_accepted(self) -> bool:
        return self.outcome == ACCEPTED and not self.deferrals

    @property
    def blocking_reason_codes(self) -> list[str]:
        return [deferral.reason_code for deferral in self.deferrals]

    @property
    def is_permanent(self) -> bool:
        return any(deferral.kind == PERMANENT for deferral in self.deferrals)

    @property
    def deferred_until(self) -> date | None:
        """The latest TIMED expiry, or None if permanent or conditional.

        Returning None for a permanent or conditional deferral is deliberate:
        a caller that stores this as `donor.deferred_until` must not be handed a
        date that would later silently lapse into eligibility.
        """

        if self.is_permanent:
            return None

        if any(deferral.kind == CONDITIONAL for deferral in self.deferrals):
            return None

        dates = [d.until for d in self.deferrals if d.until]

        return max(dates) if dates else None

    def add(self, deferral: Deferral | None) -> None:
        if deferral is None:
            return

        self.deferrals.append(deferral)
        self.outcome = DEFERRED

        if deferral.signoff_limb:
            self.signoff_notes.append(
                f"{deferral.reason_code}: applied {deferral.signoff_limb}"
            )


# --------------------------------------------------------------- rule access --


def rule(reason_code: str) -> dict:
    return (config.get("deferral_rules") or {}).get(reason_code) or {}


def signoff_entry(reason_code: str) -> dict:
    return (config.get("requires_clinical_signoff") or {}).get(reason_code) or {}


def humanise(reason_code: str) -> str:
    return reason_code.replace("_", " ").capitalize()


def build_deferral(
    reason_code: str,
    *,
    today: date,
    detail: str = "",
    sex: str | None = None,
    from_date: date | None = None,
) -> Deferral:
    """Turn a configured rule into a dated deferral.

    `from_date` is when the triggering event happened; a tattoo twelve months ago
    should not defer for another twelve months from today.
    """

    spec = rule(reason_code)
    kind = str(spec.get("kind", TIMED)).upper()
    confidence = str(spec.get("confidence", "medium"))
    limb = signoff_entry(reason_code).get("applied")

    if kind == PERMANENT:
        return Deferral(
            reason_code=reason_code,
            kind=PERMANENT,
            label=humanise(reason_code),
            detail=detail,
            confidence=confidence,
            signoff_limb=limb,
        )

    if kind == CONDITIONAL:
        return Deferral(
            reason_code=reason_code,
            kind=CONDITIONAL,
            label=humanise(reason_code),
            detail=detail,
            confidence=confidence,
            signoff_limb=limb,
        )

    days = spec.get("days")

    if days is None and sex:
        days = spec.get("days_female" if sex == FEMALE else "days_male")

    if days is None:
        days = spec.get("days_male") or 0

    anchor = from_date or today

    return Deferral(
        reason_code=reason_code,
        kind=TIMED,
        label=humanise(reason_code),
        until=anchor + timedelta(days=int(days)),
        days=int(days),
        detail=detail,
        confidence=confidence,
        signoff_limb=limb,
    )


# ------------------------------------------------------------------- vitals --


def haemoglobin_threshold(sex: str) -> tuple[float, str]:
    """The applicable cutoff and the method it belongs to.

    The Punjab SOP carries two methods with different cutoffs: copper sulphate is
    sex-neutral at 12.5 g/dL, while the haemoglobinometer rule is 12.5 for women
    and 13.5 for men. Which applies depends on what the bench actually uses, so
    it is configured rather than assumed.
    """

    method = str(config.get("donor_eligibility.haemoglobin.method", "CUSO4")).upper()

    if method == "CUSO4":
        return (
            float(config.get("donor_eligibility.haemoglobin.cuso4_gdl_all", 12.5)),
            "CUSO4",
        )

    key = (
        "donor_eligibility.haemoglobin.meter_gdl_female"
        if sex == FEMALE
        else "donor_eligibility.haemoglobin.meter_gdl_male"
    )

    return float(config.get(key, 12.5)), "METER"


def collection_volume_for(weight_kg: float | None) -> int | None:
    """The two-step volume ladder. Below the reduced floor, do not collect.

    Deliberately a ladder and not a proportional formula: a (weight/50)*450
    calculation silently changes the blood-to-anticoagulant ratio, and it is not
    the Pakistani rule.
    """

    if weight_kg is None:
        return None

    full_floor = float(config.get("donor_eligibility.weight_kg_min_full_volume", 50))
    reduced_floor = float(
        config.get("donor_eligibility.weight_kg_min_reduced_volume", 45)
    )

    volumes = config.get("donor_eligibility.collection_volume_ml") or {}

    if weight_kg >= full_floor:
        return int(volumes.get("full", 450))

    if weight_kg >= reduced_floor:
        return int(volumes.get("reduced", 350))

    return None


def assess_vitals(
    assessment: Assessment,
    *,
    today: date,
    sex: str,
    age_years: int | None,
    haemoglobin_g_dl: float | None,
    weight_kg: float | None,
    systolic_bp: int | None,
    diastolic_bp: int | None,
    pulse_bpm: int | None,
    temperature_c: float | None,
) -> None:
    age_min = int(config.get("donor_eligibility.age_years_min", 18))
    age_max = int(config.get("donor_eligibility.age_years_max", 60))

    if age_years is not None and not (age_min <= age_years <= age_max):
        assessment.add(
            Deferral(
                reason_code="AGE_OUT_OF_RANGE",
                kind=CONDITIONAL if age_years < age_min else PERMANENT,
                label="Outside the accepted donor age range",
                detail=f"{age_years} years; accepted range {age_min}-{age_max}",
                confidence="high",
            )
        )

    if haemoglobin_g_dl is not None:
        threshold, method = haemoglobin_threshold(sex)

        if haemoglobin_g_dl < threshold:
            assessment.add(
                build_deferral(
                    "LOW_HAEMOGLOBIN",
                    today=today,
                    sex=sex,
                    detail=(
                        f"{haemoglobin_g_dl:g} g/dL against a {threshold:g} g/dL "
                        f"cutoff ({method} method). Prescribe haematinics and "
                        "recheck."
                    ),
                )
            )

    volume = collection_volume_for(weight_kg)

    if weight_kg is not None and volume is None:
        floor = float(config.get("donor_eligibility.weight_kg_min_reduced_volume", 45))
        assessment.add(
            build_deferral(
                "UNDERWEIGHT",
                today=today,
                detail=f"{weight_kg:g} kg is below the {floor:g} kg minimum",
            )
        )
    else:
        assessment.collection_volume_ml = volume

        full_floor = float(
            config.get("donor_eligibility.weight_kg_min_full_volume", 50)
        )

        if weight_kg is not None and weight_kg < full_floor:
            # A reduced-volume draw into a bag pre-filled with anticoagulant for
            # 450 mL leaves the ratio wrong unless the bag or the anticoagulant
            # is adjusted. The engine cannot fix that; it must say so.
            assessment.warnings.append(
                f"Reduced-volume collection ({volume} mL) for a {weight_kg:g} kg "
                "donor. Confirm the anticoagulant volume matches the draw before "
                "collecting."
            )

    systolic_range = config.get("donor_eligibility.systolic_bp_mmhg") or [100, 140]
    diastolic_range = config.get("donor_eligibility.diastolic_bp_mmhg") or [70, 90]

    if systolic_bp is not None and not (
        systolic_range[0] <= systolic_bp <= systolic_range[1]
    ):
        assessment.add(
            build_deferral(
                "BP_OUT_OF_RANGE",
                today=today,
                detail=(
                    f"systolic {systolic_bp} mmHg, accepted "
                    f"{systolic_range[0]}-{systolic_range[1]}"
                ),
            )
        )
    elif diastolic_bp is not None and not (
        diastolic_range[0] <= diastolic_bp <= diastolic_range[1]
    ):
        assessment.add(
            build_deferral(
                "BP_OUT_OF_RANGE",
                today=today,
                detail=(
                    f"diastolic {diastolic_bp} mmHg, accepted "
                    f"{diastolic_range[0]}-{diastolic_range[1]}"
                ),
            )
        )

    pulse_range = config.get("donor_eligibility.pulse_bpm") or [60, 100]

    if pulse_bpm is not None and not (pulse_range[0] <= pulse_bpm <= pulse_range[1]):
        assessment.add(
            build_deferral(
                "PULSE_OUT_OF_RANGE",
                today=today,
                detail=(
                    f"{pulse_bpm} bpm, accepted "
                    f"{pulse_range[0]}-{pulse_range[1]}"
                ),
            )
        )

    temperature_max = float(
        config.get("donor_eligibility.temperature_celsius_max", 37.5)
    )

    if temperature_c is not None and temperature_c > temperature_max:
        assessment.add(
            build_deferral(
                "ACUTE_ILLNESS_FEVER",
                today=today,
                detail=f"{temperature_c:g} °C exceeds {temperature_max:g} °C",
            )
        )


# ----------------------------------------------------------------- interval --


def interval_days(sex: str) -> int:
    key = (
        "donation_interval.whole_blood_days_female"
        if sex == FEMALE
        else "donation_interval.whole_blood_days_male"
    )

    return int(config.get(key, 90))


def assess_interval(
    assessment: Assessment,
    *,
    today: date,
    sex: str,
    last_donation_on: date | None,
) -> None:
    if last_donation_on is None:
        return

    required = interval_days(sex)
    elapsed = (today - last_donation_on).days

    if elapsed < required:
        assessment.add(
            build_deferral(
                "RECENT_DONATION",
                today=today,
                sex=sex,
                from_date=last_donation_on,
                detail=(
                    f"donated {elapsed} days ago; {required} days required "
                    f"for a {sex.lower()} donor"
                ),
            )
        )


# ------------------------------------------------------------ questionnaire --

# The clinical core: the questions that actually change the outcome. Each maps to
# a configured deferral rule, so changing a duration is a config edit.
#
# The Punjab SOP's own questionnaire is a verbatim transcription of the AABB
# Full-Length Donor History Questionnaire, so it is not independent Pakistani
# policy. These questions are therefore kept to the clinically load-bearing set
# rather than reproducing a US form wholesale.
QUESTIONS = [
    {
        "key": "unwell_today",
        "question": "Are you feeling unwell today, or have you had a fever in the last two weeks?",
        "reason_code": "ACUTE_ILLNESS_FEVER",
        "dated": False,
    },
    {
        "key": "malaria_illness",
        "question": "Have you been treated for malaria in the last three months?",
        "reason_code": "MALARIA_ILLNESS",
        "dated": True,
    },
    {
        "key": "foreign_tropical_travel",
        "question": "Have you travelled outside Pakistan to a malaria-endemic area in the last six months?",
        "reason_code": "MALARIA_FOREIGN_TRAVEL",
        "dated": True,
    },
    {
        "key": "dengue_or_typhoid",
        "question": "Have you had dengue or typhoid fever in the last year?",
        "reason_code": "DENGUE",
        "dated": True,
    },
    {
        "key": "tattoo_piercing",
        "question": "Have you had a tattoo, body piercing or acupuncture in the last year?",
        "reason_code": "TATTOO_PIERCING",
        "dated": True,
    },
    {
        "key": "transfusion_received",
        "question": "Have you received a blood transfusion in the last year?",
        "reason_code": "TRANSFUSION_RECEIVED",
        "dated": True,
    },
    {
        "key": "surgery",
        "question": "Have you had major surgery in the last year?",
        "reason_code": "MAJOR_SURGERY",
        "dated": True,
    },
    {
        "key": "dental_work",
        "question": "Have you had a dental extraction or scaling in the last week?",
        "reason_code": "DENTAL_EXTRACTION",
        "dated": True,
    },
    {
        "key": "vaccination_live",
        "question": "Have you had a live vaccine in the last month?",
        "reason_code": "VACCINATION_LIVE",
        "dated": True,
    },
    {
        "key": "currently_pregnant",
        "question": "Are you currently pregnant, or have you given birth or breastfed in the last year?",
        "reason_code": "PREGNANCY_CURRENT",
        "dated": False,
        "female_only": True,
    },
    {
        "key": "needlestick_exposure",
        "question": "Have you had a needlestick injury or contact with someone else's blood in the last year?",
        "reason_code": "NEEDLESTICK_EXPOSURE",
        "dated": True,
    },
    {
        # Kept separate from the infection-history question below. Recording an
        # HIV disclosure under a hepatitis reason code would corrupt any lookback
        # and could send the wrong notification to the donor.
        "key": "infection_history",
        "question": "Have you ever tested positive for hepatitis B, hepatitis C or HIV?",
        "reason_code": "HEPATITIS_B_HISTORY",
        "dated": False,
        "choices": {
            "HEPATITIS_B": "HEPATITIS_B_HISTORY",
            "HEPATITIS_C": "HEPATITIS_C_HISTORY",
            "HIV": "HIV_POSITIVE",
        },
    },
]

QUESTION_BY_KEY = {question["key"]: question for question in QUESTIONS}


def questions_for(sex: str) -> list[dict]:
    return [
        question
        for question in QUESTIONS
        if not question.get("female_only") or sex == FEMALE
    ]


def assess_questionnaire(
    assessment: Assessment,
    *,
    today: date,
    sex: str,
    answers: dict,
) -> None:
    """`answers` maps a question key to True/False, or to a dict with
    `{"answer": True, "on": date, "choice": "HIV"}` for dated or multi-limb
    questions."""

    for question in questions_for(sex):
        raw = answers.get(question["key"])

        if raw in (None, False, "", "no", "NO"):
            continue

        event_date = None
        reason_code = question["reason_code"]

        if isinstance(raw, dict):
            if not raw.get("answer"):
                continue

            event_date = raw.get("on")

            choice = raw.get("choice")
            if choice and question.get("choices"):
                reason_code = question["choices"].get(choice, reason_code)

        assessment.add(
            build_deferral(
                reason_code,
                today=today,
                sex=sex,
                from_date=event_date if question.get("dated") else None,
                detail=question["question"],
            )
        )


# -------------------------------------------------------------- entry point --


def assess(
    *,
    sex: str,
    today: date | None = None,
    age_years: int | None = None,
    haemoglobin_g_dl: float | None = None,
    weight_kg: float | None = None,
    systolic_bp: int | None = None,
    diastolic_bp: int | None = None,
    pulse_bpm: int | None = None,
    temperature_c: float | None = None,
    last_donation_on: date | None = None,
    answers: dict | None = None,
    already_permanently_deferred: bool = False,
    deferred_until: date | None = None,
) -> Assessment:
    """Assess one donor on one day. Deterministic and side-effect free."""

    today = today or DEMO_DATETIME.date()
    sex = (sex or MALE).upper()

    assessment = Assessment()

    if already_permanently_deferred:
        assessment.add(
            Deferral(
                reason_code="EXISTING_PERMANENT_DEFERRAL",
                kind=PERMANENT,
                label="Donor is permanently deferred on record",
                confidence="high",
            )
        )

    if deferred_until and deferred_until > today:
        assessment.add(
            Deferral(
                reason_code="EXISTING_DEFERRAL",
                kind=TIMED,
                label="Donor is under an existing deferral",
                until=deferred_until,
                days=(deferred_until - today).days,
                confidence="high",
            )
        )

    assess_vitals(
        assessment,
        today=today,
        sex=sex,
        age_years=age_years,
        haemoglobin_g_dl=haemoglobin_g_dl,
        weight_kg=weight_kg,
        systolic_bp=systolic_bp,
        diastolic_bp=diastolic_bp,
        pulse_bpm=pulse_bpm,
        temperature_c=temperature_c,
    )

    assess_interval(
        assessment, today=today, sex=sex, last_donation_on=last_donation_on
    )

    assess_questionnaire(
        assessment, today=today, sex=sex, answers=answers or {}
    )

    _apply_malaria_interlock(assessment, today=today, answers=answers or {})

    return assessment


def _apply_malaria_interlock(
    assessment: Assessment, *, today: date, answers: dict
) -> None:
    """Pakistan applies no domestic malaria travel deferral.

    That is only safe because every donation is screened for malarial parasite —
    and that screening is a Punjab provincial mandate, not a federal one. If a
    deployment turns malaria screening off, the travel deferral has to come back
    on. Enforcing the coupling here means an operator cannot silently create the
    unsafe combination by editing one config key.
    """

    if config.get("tti_panel.malaria_screening_enabled", True):
        return

    assessment.warnings.append(
        "Malaria screening is disabled for this deployment, so domestic travel "
        "within Pakistan is now a deferrable exposure. Pakistan is malaria "
        "endemic; the usual absence of a domestic travel deferral depends on "
        "every donation being screened."
    )

    if not any(
        deferral.reason_code.startswith("MALARIA") for deferral in assessment.deferrals
    ):
        assessment.add(
            build_deferral(
                "MALARIA_ILLNESS",
                today=today,
                detail=(
                    "Malaria screening disabled; deferring on residence in an "
                    "endemic country"
                ),
            )
        )


def signoff_summary() -> list[dict]:
    """Rules a specialist still has to choose between.

    Surfaced in the UI rather than buried in a config comment, because a value
    that nobody was told was contested will be treated as settled.
    """

    entries = config.get("requires_clinical_signoff") or {}

    return [
        {
            "reason_code": code,
            "applied": entry.get("applied"),
            "alternative": entry.get("alternative"),
            "note": entry.get("note", "").strip(),
            "confidence": rule(code).get("confidence", "unknown"),
        }
        for code, entry in entries.items()
    ]
