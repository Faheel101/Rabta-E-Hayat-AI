"""Component processing: separating a bag into the products it yields.

Two facts about separation drive everything here, and both live in
`config/network.yaml` rather than in this file.

**It is time-critical, per component.** Platelets must come off within about
eight hours — before the unit is chilled — and lose function quickly at 4°C.
Red cells and plasma have a day. So a bag processed late is not a wasted bag: it
is a bag that yields red cells and plasma but no platelet. The window is enforced
per component for exactly that reason, and the platelet that could not be made is
recorded as a loss with its cause rather than quietly never existing.

**It is lossy.** A failed spin, a lipaemic unit, an under-filled bag. The
generator previously recorded `units_expected == units_produced` on every one of
68,215 separations, which asserts a processing loss rate of zero — a figure no
blood bank has, and one that makes the yield report unreadable because it can
only ever say 100%.

Units are created HERE, not at collection. A bag exists from the moment it is
drawn; the products it separates into exist when somebody spins it.

**Separation comes before release, not after.** The panel takes longer than the
platelet window, so a bank that waited for results before spinning would lose
every platelet it collected. Components separated from an untested bag are
created quarantined and become issuable when the lab releases the donation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Permission
from core import config
from core.clock import DEMO_DATETIME
from db.models import (
    BloodUnit,
    Component,
    ComponentProduction,
    Donation,
    Donor,
)
from services.audit import Actor, ServiceError, audited, require, snapshot

PRODUCTION_FIELDS = (
    "donation_id",
    "facility_id",
    "produced_at",
    "method",
    "recipe_code",
    "units_expected",
    "units_produced",
    "minutes_from_collection",
    "produced_by",
)


def recipes() -> dict[str, list[str]]:
    return {
        code: list(components)
        for code, components in (config.get("processing.recipes") or {}).items()
    }


def separation_windows() -> dict[str, int]:
    return dict(config.get("processing.separation_window_hours") or {})


def loss_reasons_for(component_code: str) -> list[str]:
    table = config.get("processing.loss_reasons") or {}

    return list(table.get(component_code) or ["BAG_DAMAGED"])


def expected_components(bag_type: str | None) -> list[str]:
    """What the kit chosen at collection was meant to yield."""

    return recipes().get(str(bag_type or "").upper(), ["WB"])


def window_status(component_code: str, collected_at, at=None) -> dict:
    """Whether this component can still be separated from that bag.

    Returned rather than raised so the screen can show a technologist which
    products are still available before they commit to anything.
    """

    at = at or DEMO_DATETIME
    hours = separation_windows().get(component_code)
    elapsed = (at - collected_at).total_seconds() / 3600.0

    if hours is None:
        return {"allowed": True, "hours_elapsed": round(elapsed, 1), "limit": None}

    return {
        "allowed": elapsed <= hours,
        "hours_elapsed": round(elapsed, 1),
        "limit": hours,
        "hours_remaining": round(max(0.0, hours - elapsed), 1),
    }


# ------------------------------------------------------------------ worklist


@dataclass
class PendingSeparation:
    """A released donation whose bag has not been separated."""

    donation_id: str
    din: str
    collected_at: object
    bag_type: str
    donor_code: str | None
    expected: list[str] = field(default_factory=list)
    available: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)

    @property
    def fully_available(self) -> bool:
        return not self.expired


def pending(db: Session, actor: Actor, *, limit: int = 200) -> list[PendingSeparation]:
    """Donations released by the lab but not yet separated.

    Ordered by how soon a component window closes rather than by age, because
    the useful ordering is "what will I lose first", not "what arrived first".
    """

    rows = db.execute(
        select(
            Donation.id,
            Donation.din,
            Donation.collected_at,
            Donation.bag_type,
            Donor.donor_code,
        )
        .join(Donor, Donor.id == Donation.donor_id)
        .outerjoin(
            ComponentProduction, ComponentProduction.donation_id == Donation.id
        )
        .where(
            Donation.facility_id == actor.facility_id,
            # Not RELEASED-only. Separation happens BEFORE the lab finishes —
            # a platelet has eight hours from the needle and the panel takes
            # longer than that, so waiting for release would lose every platelet.
            # The components are quarantined until release clears them.
            Donation.status.in_(("COLLECTED", "TESTING", "RELEASED")),
            ComponentProduction.id.is_(None),
        )
        .order_by(Donation.collected_at)
        .limit(limit)
    ).all()

    result = []

    for row in rows:
        expected = expected_components(row.bag_type)
        available, expired = [], []

        for code in expected:
            if window_status(code, row.collected_at)["allowed"]:
                available.append(code)
            else:
                expired.append(code)

        result.append(
            PendingSeparation(
                donation_id=row.id,
                din=row.din,
                collected_at=row.collected_at,
                bag_type=row.bag_type,
                donor_code=row.donor_code,
                expected=expected,
                available=available,
                expired=expired,
            )
        )

    # Soonest-closing window first.
    def urgency(item: PendingSeparation) -> float:
        remaining = [
            window_status(code, item.collected_at).get("hours_remaining", 999.0)
            for code in item.available
        ]

        return min(remaining) if remaining else 999.0

    return sorted(result, key=urgency)


def pending_count(db: Session, facility_id: str | None) -> int:
    if not facility_id:
        return 0

    return (
        db.scalar(
            select(func.count())
            .select_from(Donation)
            .outerjoin(
                ComponentProduction, ComponentProduction.donation_id == Donation.id
            )
            .where(
                Donation.facility_id == facility_id,
                Donation.status.in_(("COLLECTED", "TESTING", "RELEASED")),
                ComponentProduction.id.is_(None),
            )
        )
        or 0
    )


# ---------------------------------------------------------------- separation


METHODS = {
    "TRIPLE": "BUFFY_COAT",
    "DOUBLE": "CENTRIFUGATION",
    "SINGLE": "NONE",
    "APHERESIS_KIT": "APHERESIS",
}


def separate(
    db: Session,
    actor: Actor,
    *,
    donation_id: str,
    produce: list[str] | None = None,
    losses: dict[str, str] | None = None,
    notes: str | None = None,
) -> ComponentProduction:
    """Separate a bag, creating the units it actually yielded.

    `produce` is what the technologist chose to make — defaulting to the recipe
    the collection kit implies. Anything expected but not produced needs a
    reason in `losses`, because an unattributed loss is one nobody can reduce.
    """

    require(actor, Permission.PROCESS_COMPONENTS, "separate components")

    donation = db.scalars(
        select(Donation).where(
            Donation.id == donation_id,
            Donation.facility_id == actor.facility_id,
        )
    ).first()

    if donation is None:
        raise ServiceError("DONATION_NOT_FOUND", "That donation does not exist here.")

    if donation.status == "QUARANTINED":
        raise ServiceError(
            "QUARANTINED",
            "This donation is quarantined on a reactive result. Its bag must not "
            "be separated into products.",
        )

    if donation.status not in ("COLLECTED", "TESTING", "RELEASED"):
        raise ServiceError(
            "NOT_SEPARABLE",
            f"A donation in state {donation.status} cannot be separated.",
        )

    already = db.scalar(
        select(ComponentProduction.id).where(
            ComponentProduction.donation_id == donation_id
        )
    )

    if already:
        raise ServiceError(
            "ALREADY_SEPARATED", "This bag has already been separated."
        )

    parent_status = db.scalar(
        select(BloodUnit.status).where(
            BloodUnit.donation_id == donation_id, BloodUnit.din == donation.din
        )
    )

    if parent_status in ("ISSUED", "TRANSFUSED", "RESERVED", "CROSSMATCHED"):
        # Somebody has already committed this bag to a patient as whole blood.
        raise ServiceError(
            "BAG_COMMITTED",
            "This bag has already been issued as whole blood and cannot be "
            "separated.",
        )

    expected = expected_components(donation.bag_type)
    chosen = list(produce) if produce is not None else list(expected)
    losses = dict(losses or {})

    unknown = [code for code in chosen if code not in expected]

    if unknown:
        raise ServiceError(
            "NOT_IN_RECIPE",
            f"A {donation.bag_type.lower()} bag does not yield "
            + ", ".join(unknown)
            + ".",
        )

    # The window, per component. A late bag still gives red cells and plasma.
    blocked = {}

    for code in list(chosen):
        status = window_status(code, donation.collected_at)

        if not status["allowed"]:
            chosen.remove(code)
            blocked[code] = (
                f"SEPARATION_WINDOW_MISSED_{status['limit']}H"
            )

    for code in expected:
        if code not in chosen and code not in losses and code not in blocked:
            raise ServiceError(
                "LOSS_REASON_REQUIRED",
                f"{code} was expected from this bag but is not being produced. "
                "Record why — an unattributed loss is one nobody can reduce.",
                field=code,
            )

    losses.update(blocked)

    minutes = int((DEMO_DATETIME - donation.collected_at).total_seconds() / 60)

    record = ComponentProduction(
        id=str(uuid.uuid4()),
        donation_id=donation_id,
        facility_id=donation.facility_id,
        produced_at=DEMO_DATETIME,
        method=METHODS.get(str(donation.bag_type or "").upper(), "CENTRIFUGATION"),
        recipe_code=donation.bag_type,
        units_expected=len(expected),
        units_produced=len(chosen),
        expected_components=expected,
        produced_components=chosen,
        loss_reasons=losses or None,
        minutes_from_collection=minutes,
        produced_by=actor.display_name,
        notes=notes,
    )

    with audited(db, actor, "COMPONENTS_SEPARATED", "component_production") as entry:
        db.add(record)
        db.flush()

        # The parent bag stops existing as whole blood the moment it is spun.
        # Leaving it AVAILABLE alongside its own components would double-count
        # the same blood — once as a bag and again as the products made from it.
        parent = db.scalars(
            select(BloodUnit).where(
                BloodUnit.donation_id == donation_id,
                BloodUnit.din == donation.din,
            )
        ).first()

        if parent is not None:
            parent.status = "SEPARATED"
            parent.discarded_at = DEMO_DATETIME
            parent.discard_reason = "SEPARATED_INTO_COMPONENTS"

        # Components inherit the bag's state. Separated before the lab has
        # finished, they are quarantined and become available when the
        # donation is released; separated after, they are available at once.
        units = _make_units(db, donation, chosen)

        for unit in units:
            db.add(unit)

        db.flush()

        entry.on(record, after=snapshot(record, PRODUCTION_FIELDS))
        entry.note(
            expected=expected,
            produced=chosen,
            losses=losses,
            unit_dins=[unit.din for unit in units],
            minutes_from_collection=minutes,
            # Recorded because a bank that keeps missing the platelet window has
            # a scheduling problem, not a technique problem.
            window_missed=sorted(blocked),
            parent_bag_consumed=donation.din,
        )

    return record


def _make_units(db: Session, donation: Donation, codes: list[str]) -> list[BloodUnit]:
    """The products, as unit records.

    Created here rather than at collection: a bag exists from the moment it is
    drawn, but the components it separates into exist when somebody spins it.
    """

    if not codes:
        return []

    components = {
        row.code: row
        for row in db.scalars(select(Component).where(Component.code.in_(codes))).all()
    }
    donor = db.get(Donor, donation.donor_id)
    released = donation.status == "RELEASED"

    units = []

    for index, code in enumerate(codes, start=1):
        component = components.get(code)

        if component is None:
            continue

        units.append(
            BloodUnit(
                id=str(uuid.uuid4()),
                din=f"{donation.din}-{index:02d}" if len(codes) > 1 else donation.din,
                donation_id=donation.id,
                facility_id=donation.facility_id,
                component_id=component.id,
                blood_group_id=donor.blood_group_id if donor else None,
                volume_ml=_volume_for(code, donation.volume_ml, len(codes)),
                collected_at=donation.collected_at,
                # Shelf life runs from COLLECTION, not from separation. A
                # platelet spun eight hours late still expires five days after
                # the needle, and dating it from the spin would silently extend
                # every unit's life by however long processing took.
                expires_at=donation.collected_at
                + timedelta(days=int(component.shelf_life_days)),
                status="AVAILABLE" if released else "QUARANTINE",
                screening_status="PASSED" if released else "PENDING",
                is_leucodepleted=False,
                is_irradiated=False,
                cold_chain_breach_count=0,
                last_synced_at=DEMO_DATETIME,
            )
        )

    return units


# Rough product volumes. A 450 mL whole blood bag does not split evenly: most of
# the volume is red cells, plasma takes about half of what remains, and a
# random-donor platelet is a small concentrate.
VOLUME_SHARES = {
    "PRBC": 0.62,
    "FFP": 0.50,
    "PLT_RD": 0.12,
    "CRYO": 0.05,
    "PLT_APH": 1.0,
    "WB": 1.0,
}


def _volume_for(code: str, bag_volume: int | None, produced: int) -> int:
    volume = int(bag_volume or 450)

    if code in ("WB", "PLT_APH"):
        return volume

    return max(20, int(volume * VOLUME_SHARES.get(code, 1.0 / max(1, produced))))


def yield_summary(db: Session, facility_id: str, *, days: int = 30) -> dict:
    """Yield against expectation, and where the losses went.

    The figure this exists to produce: a bank that keeps losing platelets to a
    missed window has a scheduling problem it can act on, and one losing them to
    failed spins has a technique problem it cannot fix by rescheduling.
    """

    since = DEMO_DATETIME - timedelta(days=days)

    # Only separations recorded through this module. Reconstructed records have
    # no operator, because nobody was recorded — and their expected recipe was
    # set equal to what was produced rather than known, so counting them would
    # dilute a measured yield with a fabricated 100%.
    rows = db.scalars(
        select(ComponentProduction).where(
            ComponentProduction.facility_id == facility_id,
            ComponentProduction.produced_at >= since,
            ComponentProduction.produced_by.is_not(None),
        )
    ).all()

    reconstructed = (
        db.scalar(
            select(func.count())
            .select_from(ComponentProduction)
            .where(
                ComponentProduction.facility_id == facility_id,
                ComponentProduction.produced_at >= since,
                ComponentProduction.produced_by.is_(None),
            )
        )
        or 0
    )

    expected = sum(row.units_expected or 0 for row in rows)
    produced = sum(row.units_produced or 0 for row in rows)

    by_reason: dict[str, int] = {}
    by_component: dict[str, int] = {}

    for row in rows:
        for code, reason in (row.loss_reasons or {}).items():
            by_reason[reason] = by_reason.get(reason, 0) + 1
            by_component[code] = by_component.get(code, 0) + 1

    return {
        "separations": len(rows),
        # Stated rather than hidden: a facility whose yield is based on four
        # separations should know that before acting on the percentage.
        "reconstructed_excluded": reconstructed,
        "units_expected": expected,
        "units_produced": produced,
        "units_lost": expected - produced,
        "yield_rate": (produced / expected) if expected else 0.0,
        "losses_by_reason": sorted(
            by_reason.items(), key=lambda item: -item[1]
        ),
        "losses_by_component": sorted(
            by_component.items(), key=lambda item: -item[1]
        ),
    }
