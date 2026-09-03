"""The donor register, and one donor's full record.

Two pages that do most of the work of a blood bank's front desk. The register
answers "who can I call today"; the record answers "who was this, what did we
take, and what did the lab find".

Both are scoped by `readable_facilities`: a bench user sees their own facility's
register, a group coordinator sees every facility their organisation owns, and
nobody sees past that. Donor identity is the most sensitive data in the system
and it never travels across the network layer — the cross-organisation check in
§30 returns a deferral flag and nothing else.

Eligibility shown here is computed live from `core.eligibility` rather than read
from a stored flag. A stored flag goes stale the moment the interval elapses,
and a donor wrongly shown as ineligible is a unit that never gets collected.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from core import eligibility
from core.clock import DEMO_DATETIME, as_utc
from db.models import (
    BloodGroup,
    BloodUnit,
    Component,
    Donation,
    DonationSession,
    DonationTest,
    Donor,
    DonorDeferral,
    DonorScreening,
    Facility,
)
from i18n.t import t
from services.screening import FINAL_OUTCOMES
from app.auth import Permission
from web.deps import (
    Principal,
    get_db,
    principal_can,
    require_permission,
    require_principal,
)
from web.routers.facility import ENABLED_NAV, nav_counts
from web.templating import current_lang, flash, render

router = APIRouter(
    prefix="/app/donors",
    dependencies=[Depends(require_permission(Permission.REGISTER_DONOR))],
)

PAGE_SIZE = 40

# What the register can be filtered down to. "Eligible today" is the one the
# recall desk actually uses.
AVAILABILITY_FILTERS = {
    "eligible": "Eligible to donate today",
    "interval": "Inside the donation interval",
    "deferred": "Temporarily deferred",
    "permanent": "Permanently deferred",
    "contactable": "Consented to contact",
}


def readable_facilities(principal: Principal) -> list[str]:
    """Which registers this user may read.

    A group coordinator has no home facility, so scoping them to
    `principal.facility_id` would show an empty register rather than the group's
    donors. Everyone else is confined to the bench they work at.
    """

    scope = "organization" if principal.is_group_user else "facility"
    return principal.facility_ids(scope)


def _page(request: Request, principal: Principal, db: Session, **kwargs):
    return render(
        request,
        principal=principal,
        db=db,
        nav_counts=nav_counts(db, principal),
        enabled_nav=ENABLED_NAV,
        **kwargs,
    )


def _sex(donor) -> str:
    """The eligibility engine keys several rules on sex; default conservatively.

    Where sex is unrecorded the engine is given the stricter of the two profiles
    so an unknown never produces a more permissive answer than a known.
    """

    return "FEMALE" if (donor.gender or "").upper() == "FEMALE" else "MALE"


def _age_years(date_of_birth: date | None, today: date) -> int | None:
    if date_of_birth is None:
        return None

    years = today.year - date_of_birth.year

    if (today.month, today.day) < (date_of_birth.month, date_of_birth.day):
        years -= 1

    return years


def assess_donor(donor, *, today: date) -> eligibility.Assessment:
    """Interval and age only.

    Vitals are deliberately excluded: a donor's haemoglobin from three months
    ago says nothing about today, and presenting a stale reading as a current
    eligibility decision is exactly the kind of false confidence this system
    should not manufacture. The chair-side screen measures them fresh.
    """

    assessment = eligibility.Assessment()

    eligibility.assess_vitals(
        assessment,
        today=today,
        sex=_sex(donor),
        age_years=_age_years(donor.date_of_birth, today),
        haemoglobin_g_dl=None,
        weight_kg=None,
        systolic_bp=None,
        diastolic_bp=None,
        pulse_bpm=None,
        temperature_c=None,
    )

    last = as_utc(donor.last_donation_at)

    eligibility.assess_interval(
        assessment,
        today=today,
        sex=_sex(donor),
        last_donation_on=last.date() if last else None,
    )

    return assessment


def active_deferrals(db: Session, donor_ids: list[str]) -> dict[str, list]:
    """Open deferrals per donor, newest first.

    "Open" means not lifted and not yet elapsed. A deferral with no
    `deferred_until` never elapses on its own — that is the whole point of the
    CONDITIONAL and awaiting-confirmation kinds — so it stays open until somebody
    lifts it.
    """

    if not donor_ids:
        return {}

    rows = db.execute(
        select(
            DonorDeferral.donor_id,
            DonorDeferral.reason_code,
            DonorDeferral.reason_note,
            DonorDeferral.is_permanent,
            DonorDeferral.deferred_at,
            DonorDeferral.deferred_until,
        )
        .where(
            DonorDeferral.donor_id.in_(donor_ids),
            DonorDeferral.lifted_at.is_(None),
            or_(
                DonorDeferral.is_permanent.is_(True),
                DonorDeferral.deferred_until.is_(None),
                DonorDeferral.deferred_until > DEMO_DATETIME.date(),
            ),
        )
        .order_by(DonorDeferral.deferred_at.desc())
    ).all()

    grouped: dict[str, list] = {}

    for row in rows:
        grouped.setdefault(row.donor_id, []).append(row)

    return grouped


def humanise_reason(code: str | None) -> str:
    if not code:
        return "Deferred"

    return code.replace("TTI_", "").replace("_", " ").capitalize()


def eligibility_state(donor, *, today: date, deferrals: list | None = None) -> dict:
    """A single compact verdict for the register row.

    Ordered by severity. The deferral ledger is consulted FIRST, because it is
    the record of truth: a deferral with no end date is invisible in
    `deferred_until`, and computing eligibility from that column alone is the
    mistake core/eligibility.py's deferral-kind enum exists to prevent.
    """

    for deferral in deferrals or []:
        if deferral.is_permanent:
            return {
                "code": "PERMANENT",
                "label": "Permanently deferred",
                "tone": "critical",
                "detail": (
                    deferral.reason_note
                    or f"{humanise_reason(deferral.reason_code)}. This does not expire."
                ),
                "reason_code": deferral.reason_code,
            }

    for deferral in deferrals or []:
        if deferral.deferred_until is None:
            # No end date, and not permanent: the deferral lifts when the finding
            # resolves or when a pending result arrives, not on a calendar date.
            awaiting = (deferral.reason_code or "").startswith("TTI_AWAITING")

            return {
                "code": "AWAITING_CONFIRMATION" if awaiting else "CONDITIONAL",
                "label": "Awaiting confirmation" if awaiting else "Conditional",
                "tone": "warning",
                "detail": (
                    deferral.reason_note
                    or f"{humanise_reason(deferral.reason_code)}. No automatic end "
                    "date — must be re-assessed before this donor is accepted."
                ),
                "reason_code": deferral.reason_code,
            }

        days = (deferral.deferred_until - today).days

        return {
            "code": "DEFERRED",
            "label": f"Deferred {days}d",
            "tone": "warning",
            "detail": (
                f"{humanise_reason(deferral.reason_code)}. Eligible again "
                f"{deferral.deferred_until:%d %b %Y}."
            ),
            "until": deferral.deferred_until,
            "days": days,
            "reason_code": deferral.reason_code,
        }

    # Fall back to the donor columns for register entries migrated from before
    # go-live, which have no deferral row behind them.
    if donor.is_permanently_deferred:
        return {
            "code": "PERMANENT",
            "label": "Permanently deferred",
            "tone": "critical",
            "detail": "Not eligible to donate. This does not expire.",
        }

    deferred_until = donor.deferred_until

    if deferred_until and deferred_until > today:
        return {
            "code": "DEFERRED",
            "label": f"Deferred {(deferred_until - today).days}d",
            "tone": "warning",
            "detail": f"Deferred until {deferred_until:%d %b %Y}.",
            "until": deferred_until,
            "days": (deferred_until - today).days,
        }

    last = as_utc(donor.last_donation_at)
    interval = eligibility.interval_days(_sex(donor))

    if last is not None:
        next_eligible = last.date() + timedelta(days=interval)

        if next_eligible > today:
            return {
                "code": "INTERVAL",
                "label": f"{(next_eligible - today).days}d to go",
                "tone": "neutral",
                "detail": (
                    f"Last donated {last:%d %b %Y}. The {interval}-day interval "
                    f"ends {next_eligible:%d %b %Y}."
                ),
                "until": next_eligible,
                "days": (next_eligible - today).days,
                "last_donation_on": last.date(),
                "interval_days": interval,
            }

    age = _age_years(donor.date_of_birth, today)
    minimum = int(eligibility.config.get("donor_eligibility.age_years_min"))
    maximum = int(eligibility.config.get("donor_eligibility.age_years_max"))

    if age is not None and not (minimum <= age <= maximum):
        return {
            "code": "AGE",
            "label": "Outside age range",
            "tone": "warning",
            "detail": f"Aged {age}; the register accepts {minimum}–{maximum}.",
            "age": age,
            "minimum_age": minimum,
            "maximum_age": maximum,
        }

    return {
        "code": "ELIGIBLE",
        "label": "Eligible",
        "tone": "success",
        "detail": "Clear on interval, age and deferrals. Vitals are checked at the chair.",
    }


@router.get("")
def register(
    request: Request,
    q: str = Query("", description="Name, donor code or CNIC last four"),
    group: str = Query(""),
    donor_type: str = Query(""),
    availability: str = Query(""),
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    lang = current_lang(request)
    today = DEMO_DATETIME.date()
    facility_ids = readable_facilities(principal)

    statement = select(Donor).where(Donor.registered_facility_id.in_(facility_ids))

    if q:
        needle = f"%{q.strip()}%"
        statement = statement.where(
            or_(
                Donor.full_name.ilike(needle),
                Donor.donor_code.ilike(needle),
                Donor.cnic_last4 == q.strip()[-4:],
                Donor.phone.ilike(needle),
            )
        )

    if group:
        statement = statement.join(
            BloodGroup, BloodGroup.id == Donor.blood_group_id
        ).where(BloodGroup.code == group)

    if donor_type:
        statement = statement.where(Donor.donor_type == donor_type)

    # Availability is expressed in SQL rather than filtered in Python, so the
    # count in the header is the real count and not the count of one page.
    open_deferral = (
        select(DonorDeferral.id)
        .where(
            DonorDeferral.donor_id == Donor.id,
            DonorDeferral.lifted_at.is_(None),
        )
    )

    if availability == "permanent":
        statement = statement.where(
            or_(
                Donor.is_permanently_deferred.is_(True),
                open_deferral.where(DonorDeferral.is_permanent.is_(True)).exists(),
            )
        )
    elif availability == "deferred":
        statement = statement.where(
            Donor.is_permanently_deferred.is_(False),
            or_(
                and_(
                    Donor.deferred_until.is_not(None),
                    Donor.deferred_until > DEMO_DATETIME.date(),
                ),
                open_deferral.where(
                    DonorDeferral.is_permanent.is_(False),
                    or_(
                        DonorDeferral.deferred_until.is_(None),
                        DonorDeferral.deferred_until > DEMO_DATETIME.date(),
                    ),
                ).exists(),
            ),
        )
    elif availability == "contactable":
        statement = statement.where(Donor.consent_contact.is_(True))
    elif availability in ("eligible", "interval"):
        # The interval is sex-dependent, so it cannot be a single constant.
        male_cutoff = DEMO_DATETIME - timedelta(
            days=eligibility.interval_days("MALE")
        )
        female_cutoff = DEMO_DATETIME - timedelta(
            days=eligibility.interval_days("FEMALE")
        )
        clear_of_interval = or_(
            Donor.last_donation_at.is_(None),
            (Donor.gender == "FEMALE") & (Donor.last_donation_at <= female_cutoff),
            (Donor.gender != "FEMALE") & (Donor.last_donation_at <= male_cutoff),
        )

        # An open deferral in the ledger disqualifies, whatever the donor
        # columns say. Without this the filter returned 2,244 donors of whom
        # only 30 were genuinely available, and a recall officer would have
        # phoned 1,014 conditionally-deferred people.
        no_open_deferral = ~select(DonorDeferral.id).where(
            DonorDeferral.donor_id == Donor.id,
            DonorDeferral.lifted_at.is_(None),
            or_(
                DonorDeferral.is_permanent.is_(True),
                DonorDeferral.deferred_until.is_(None),
                DonorDeferral.deferred_until > DEMO_DATETIME.date(),
            ),
        ).exists()

        if availability == "eligible":
            statement = statement.where(
                Donor.is_permanently_deferred.is_(False),
                or_(
                    Donor.deferred_until.is_(None),
                    Donor.deferred_until <= DEMO_DATETIME.date(),
                ),
                no_open_deferral,
                clear_of_interval,
            )
        else:
            statement = statement.where(~clear_of_interval)

    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(page, pages)

    donors = db.scalars(
        statement.order_by(Donor.full_name, Donor.donor_code)
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()

    group_lookup = {
        row.id: row.code for row in db.scalars(select(BloodGroup)).all()
    }

    # One query for the whole page's deferrals rather than one per row.
    deferrals_by_donor = active_deferrals(db, [donor.id for donor in donors])

    rows = [
        {
            "donor": donor,
            "group": group_lookup.get(donor.blood_group_id),
            "state": eligibility_state(
                donor, today=today, deferrals=deferrals_by_donor.get(donor.id)
            ),
        }
        for donor in donors
    ]

    # Register-wide counters, so the page opens with the numbers the recall desk
    # needs rather than making them filter to find out.
    summary = {
        "total": db.scalar(
            select(func.count())
            .select_from(Donor)
            .where(Donor.registered_facility_id.in_(facility_ids))
        )
        or 0,
        "contactable": db.scalar(
            select(func.count())
            .select_from(Donor)
            .where(
                Donor.registered_facility_id.in_(facility_ids),
                Donor.consent_contact.is_(True),
                Donor.is_permanently_deferred.is_(False),
            )
        )
        or 0,
        "deferred": db.scalar(
            select(func.count())
            .select_from(Donor)
            .where(
                Donor.registered_facility_id.in_(facility_ids),
                Donor.is_permanently_deferred.is_(False),
                Donor.deferred_until > DEMO_DATETIME.date(),
            )
        )
        or 0,
        "permanent": db.scalar(
            select(func.count())
            .select_from(Donor)
            .where(
                Donor.registered_facility_id.in_(facility_ids),
                Donor.is_permanently_deferred.is_(True),
            )
        )
        or 0,
    }

    open_session = db.scalar(
        select(DonationSession)
        .where(
            DonationSession.facility_id == principal.facility_id,
            DonationSession.status == "OPEN",
        )
        .order_by(DonationSession.opened_at.desc())
        .limit(1)
    )

    return _page(
        request,
        principal,
        db,
        template="app/donors.html",
        context={
            "rows": rows,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": PAGE_SIZE,
            "summary": summary,
            "groups": [row.code for row in db.scalars(select(BloodGroup)).all()],
            "donor_types": ["VOLUNTARY", "REPLACEMENT", "DIRECTED", "AUTOLOGOUS"],
            "availability_filters": AVAILABILITY_FILTERS,
            "open_session": open_session,
            "filters": {
                "q": q,
                "group": group,
                "donor_type": donor_type,
                "availability": availability,
            },
        },
        page_title=t("nav.donors", language=lang),
        breadcrumbs=[
            {"label": t("nav.dashboard", language=lang), "url": "/app/dashboard"},
            {"label": t("nav.donors", language=lang), "url": "/app/donors"},
        ],
    )


@router.get("/{donor_id}")
def record(
    donor_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    lang = current_lang(request)
    today = DEMO_DATETIME.date()

    donor = db.scalars(
        select(Donor).where(
            Donor.id == donor_id,
            # The tenancy check and the lookup are one query. Splitting them
            # into "find, then check" is how cross-tenant leaks happen, and a
            # 404 rather than a 403 avoids confirming the record exists at all.
            Donor.registered_facility_id.in_(readable_facilities(principal)),
        )
    ).first()

    if donor is None:
        raise HTTPException(status_code=404, detail="Donor not found")

    group = db.scalar(
        select(BloodGroup.code).where(BloodGroup.id == donor.blood_group_id)
    )

    donor_deferrals = active_deferrals(db, [donor.id]).get(donor.id, [])

    donations = db.execute(
        select(
            Donation.id,
            Donation.din,
            Donation.collected_at,
            Donation.donation_type,
            Donation.bag_type,
            Donation.volume_ml,
            Donation.status,
            Donation.adverse_reaction,
            Donation.phlebotomist,
            Donation.released_at,
            DonationSession.name.label("session_name"),
            DonationSession.session_type,
        )
        .outerjoin(DonationSession, DonationSession.id == Donation.session_id)
        .where(Donation.donor_id == donor.id)
        .order_by(Donation.collected_at.desc())
    ).all()

    donation_ids = [row.id for row in donations]

    # Test results and the units each donation produced, fetched once and
    # grouped in Python rather than queried per row.
    tests_by_donation: dict[str, list] = {}
    units_by_donation: dict[str, list] = {}

    if donation_ids:
        for row in db.execute(
            select(
                DonationTest.donation_id,
                DonationTest.test_code,
                DonationTest.result,
                DonationTest.is_reactive,
                DonationTest.method,
                DonationTest.tested_at,
                DonationTest.tested_by,
                DonationTest.verified_by,
            )
            .where(DonationTest.donation_id.in_(donation_ids))
            .order_by(DonationTest.test_code)
        ).all():
            tests_by_donation.setdefault(row.donation_id, []).append(row)

        for row in db.execute(
            select(
                BloodUnit.donation_id,
                BloodUnit.din,
                BloodUnit.status,
                BloodUnit.expires_at,
                BloodUnit.discard_reason,
                Component.code.label("component_code"),
            )
            .join(Component, Component.id == BloodUnit.component_id)
            .where(BloodUnit.donation_id.in_(donation_ids))
            .order_by(Component.id)
        ).all():
            units_by_donation.setdefault(row.donation_id, []).append(row)

    screenings = db.execute(
        select(
            DonorScreening.screened_at,
            DonorScreening.outcome,
            DonorScreening.haemoglobin_g_dl,
            DonorScreening.weight_kg,
            DonorScreening.systolic_bp,
            DonorScreening.diastolic_bp,
            DonorScreening.pulse_bpm,
            DonorScreening.temperature_c,
            DonorScreening.deferral_reason_code,
            DonorScreening.deferral_days,
            DonorScreening.screened_by,
        )
        .where(
            DonorScreening.donor_id == donor.id,
            # A draft is somebody mid-way through the wizard, not a screening
            # that happened. Showing one here would put half-entered vitals on
            # the donor's clinical record.
            DonorScreening.outcome.in_(FINAL_OUTCOMES),
        )
        .order_by(DonorScreening.screened_at.desc())
        .limit(12)
    ).all()

    facility = db.scalar(
        select(Facility.name_en).where(Facility.id == donor.registered_facility_id)
    )

    return _page(
        request,
        principal,
        db,
        template="app/donor_record.html",
        context={
            "donor": donor,
            "group": group,
            "facility_name": facility,
            "age": _age_years(donor.date_of_birth, today),
            "state": eligibility_state(
                donor, today=today, deferrals=donor_deferrals
            ),
            "deferrals": donor_deferrals,
            "assessment": assess_donor(donor, today=today),
            "donations": donations,
            "tests_by_donation": tests_by_donation,
            "units_by_donation": units_by_donation,
            "screenings": screenings,
            "now": DEMO_DATETIME,
            "can_trace_units": principal_can(
                principal, Permission.VIEW_LOCAL_INVENTORY
            ),
        },
        page_title=donor.full_name or donor.donor_code,
        breadcrumbs=[
            {"label": t("nav.dashboard", language=lang), "url": "/app/dashboard"},
            {"label": t("nav.donors", language=lang), "url": "/app/donors"},
            {
                "label": donor.full_name or donor.donor_code,
                "url": f"/app/donors/{donor.id}",
            },
        ],
    )


@router.post("/register")
def register(
    request: Request,
    full_name: str = Form(...),
    gender: str = Form("MALE"),
    date_of_birth: str = Form(...),
    blood_group_id: str = Form(""),
    phone: str = Form(""),
    cnic_last4: str = Form(""),
    donor_type: str = Form("REPLACEMENT"),
    consent_contact: str = Form(""),
    session_id: str = Form(""),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_principal),
):
    """Register a donor, and drop straight into screening if a session sent us.

    At a camp the registration form is reached from the chair, so returning to a
    donor list would put a step back in front of somebody with a queue behind
    them.
    """

    from datetime import date as _date

    from services import screening as screening_service
    from services.audit import Actor, PermissionDenied, ServiceError

    try:
        born = _date.fromisoformat(date_of_birth)
    except (TypeError, ValueError):
        flash(
            request,
            t("ops.invalid_date_of_birth", language=current_lang(request)),
            "error",
        )
        return RedirectResponse(
            f"/app/sessions/{session_id}/screen" if session_id else "/app/donors",
            status_code=303,
        )

    try:
        donor = screening_service.register_donor(
            db,
            Actor.from_principal(principal, request),
            full_name=full_name,
            gender=gender,
            date_of_birth=born,
            blood_group_id=int(blood_group_id) if blood_group_id else None,
            phone=phone or None,
            cnic_last4=cnic_last4 or None,
            donor_type=donor_type,
            consent_contact=bool(consent_contact),
        )
    except (ServiceError, PermissionDenied) as error:
        flash(request, error.message, "error")
        return RedirectResponse(
            f"/app/sessions/{session_id}/screen" if session_id else "/app/donors",
            status_code=303,
        )

    if not session_id:
        flash(
            request,
            t(
                "ops.donor_added_flash",
                language=current_lang(request),
                name=donor.full_name,
            ),
            "success",
        )
        return RedirectResponse(f"/app/donors/{donor.id}", status_code=303)

    try:
        draft = screening_service.start_screening(
            db,
            Actor.from_principal(principal, request),
            donor_id=donor.id,
            session_id=session_id,
        )
    except (ServiceError, PermissionDenied) as error:
        flash(request, error.message, "error")
        return RedirectResponse(f"/app/donors/{donor.id}", status_code=303)

    return RedirectResponse(
        f"/app/sessions/{session_id}/screen/{draft.id}", status_code=303
    )
