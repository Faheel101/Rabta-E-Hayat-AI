"""Request dependencies: database session, signed-in principal, tenant scope.

The tenant boundary is enforced here and nowhere else. Every route that reads
operational data takes a `Principal` and asks it which facilities are readable;
no router builds that list itself. One place to get it right is the only way it
stays right as modules are added.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from core.clock import as_utc
from app.auth import (
    CurrentUser,
    ROLE_MAX_SCOPE,
    Role,
    Scope,
    allowed_scopes,
    facility_ids_in_scope,
)
from db.models import (
    EmergencyIncident,
    Facility,
    Organization,
    UserAccount,
    UserSession,
)
from db.session import SessionLocal
from web import security


def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


@dataclass
class Principal:
    """Who is signed in, and what they are allowed to see."""

    user: UserAccount
    organization: Organization
    session: UserSession
    active_facility: Facility | None = None
    org_facilities: list[Facility] = field(default_factory=list)
    selected_scope: Scope = Scope.OWN_FACILITY
    scope_facilities: list[Facility] = field(default_factory=list)
    emergency_declared: bool = False

    @property
    def user_id(self) -> str:
        return self.user.id

    @property
    def role(self) -> str:
        return self.user.role

    @property
    def display_name(self) -> str:
        return self.user.full_name

    @property
    def organization_id(self) -> str:
        return self.organization.id

    @property
    def facility_id(self) -> str | None:
        return self.active_facility.id if self.active_facility else None

    @property
    def is_group_user(self) -> bool:
        """A coordinator with no home facility, working across the group."""

        return self.user.facility_id is None

    @property
    def org_facility_ids(self) -> list[str]:
        return [facility.id for facility in self.org_facilities]

    @property
    def scope_facility_ids(self) -> list[str]:
        return [facility.id for facility in self.scope_facilities]

    @property
    def selectable_scopes(self) -> list[Scope]:
        try:
            role = Role(self.role)
        except ValueError:
            return [Scope.OWN_FACILITY]
        return allowed_scopes(self.role_subject(role=role))

    def role_subject(self, *, role: Role | None = None) -> CurrentUser:
        """The single role context reused by routes, navigation and services."""

        resolved_role = role or Role(self.role)
        return CurrentUser(
            role=resolved_role,
            facility_id=self.facility_id,
            facility_name=(self.active_facility.name_en if self.active_facility else None),
            display_name=self.display_name,
            scope=self.selected_scope,
            emergency_declared=self.emergency_declared,
        )

    def facility_ids(self, scope: str = "facility") -> list[str]:
        """Facilities readable at the requested scope.

        "facility" is the active blood bank only; "organization" is every
        facility the tenant owns. Anything beyond the organization is not an
        operational read and must go through the network layer, which returns
        shared aggregates rather than records.
        """

        if scope == "scope":
            return self.scope_facility_ids

        if scope == "organization":
            return self.org_facility_ids

        return [self.facility_id] if self.facility_id else []

    def owns_facility(self, facility_id: str | None) -> bool:
        return bool(facility_id) and facility_id in set(self.org_facility_ids)

    def require_own_facility(self, facility_id: str | None) -> str:
        """Guard for any route that takes a facility id from the URL.

        Without this, changing a path parameter would read another tenant's
        operational data.
        """

        if not self.owns_facility(facility_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Not found in this organization",
            )

        return facility_id  # type: ignore[return-value]

    def require_scope_facility(self, facility_id: str | None) -> str:
        """Guard an aggregate intelligence read against the selected scope."""

        if not facility_id or facility_id not in set(self.scope_facility_ids):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Not found in the selected network scope",
            )
        return facility_id


def _load_session(db: Session, token: str) -> UserSession | None:
    return db.get(UserSession, token)


# How stale the sliding session window is allowed to get. A 30-minute idle
# timeout does not need second-accurate bookkeeping, and writing on every page
# view turns every read into a writer — see _slide_idle_window.
SESSION_TOUCH_INTERVAL = timedelta(minutes=1)


def _slide_idle_window(db: Session, session: UserSession) -> None:
    """Extend the idle window (spec §13.2), without making reads into writes.

    This used to commit on EVERY authenticated page view. SQLite in WAL mode
    guarantees that a read never blocks behind a write — but that guarantee only
    covers readers, and with this on the dependency path the application had no
    read-only page: viewing the dashboard was a write. So while any pipeline job
    (build_marts, run_forecast, run_risk_rescue) held its write transaction,
    every user request stalled for the full 30-second busy timeout and then
    returned a 500. Measured at 32 seconds before this change.

    Two things fix it. First, only touch the row when the recorded time is
    actually stale — at one-minute granularity that turns essentially every page
    view back into a pure read. Second, if the write cannot get the lock, let the
    session keep its slightly older expiry rather than failing the request: a
    user should never see an error page because a batch job is running.
    """

    now = security.now()
    last_seen = as_utc(session.last_seen_at)

    if last_seen is not None and now - last_seen < SESSION_TOUCH_INTERVAL:
        return

    session.last_seen_at = now
    session.expires_at = security.session_expiry()

    try:
        db.commit()
    except OperationalError:
        # The database is busy. The session stays valid on its previous expiry,
        # which is at worst SESSION_TOUCH_INTERVAL shorter than it should be.
        db.rollback()


def optional_principal(
    request: Request,
    db: Session = Depends(get_db),
) -> Principal | None:
    """Resolve the signed-in principal, or None. Never raises."""

    token = request.cookies.get(security.SESSION_COOKIE)

    if not token:
        return None

    session = _load_session(db, token)

    if not security.is_session_live(session):
        return None

    user = db.get(UserAccount, session.user_id)

    if user is None or not user.is_active or security.is_locked(user):
        return None

    organization = db.get(Organization, user.organization_id)

    if organization is None or not organization.is_active:
        return None

    org_facilities = list(
        db.scalars(
            select(Facility)
            .where(
                Facility.organization_id == organization.id,
                Facility.is_active.is_(True),
            )
            .order_by(Facility.name_en)
        ).all()
    )

    active_facility = None
    candidate_id = session.active_facility_id or user.facility_id

    if candidate_id:
        active_facility = next(
            (item for item in org_facilities if item.id == candidate_id), None
        )

    # A user pinned to a facility that has since been deactivated, or a group
    # user who has not chosen one yet, lands on the first facility they own
    # rather than on a page with no data and no explanation.
    if active_facility is None and org_facilities:
        active_facility = org_facilities[0]

    try:
        role = Role(user.role)
    except ValueError:
        role = Role.BLOOD_BANK_OFFICER

    ceiling = ROLE_MAX_SCOPE.get(role, Scope.OWN_FACILITY)
    selectable = allowed_scopes(
        CurrentUser(
            role=role,
            facility_id=active_facility.id if active_facility else None,
            facility_name=active_facility.name_en if active_facility else None,
            display_name=user.full_name,
        )
    )
    requested_scope = request.session.get("network_scope")
    try:
        selected_scope = Scope(requested_scope) if requested_scope else None
    except ValueError:
        selected_scope = None
    if selected_scope not in selectable:
        selected_scope = (
            ceiling
            if role
            in {
                Role.RBC_COORDINATOR,
                Role.PROVINCIAL_ADMIN,
                Role.EMERGENCY_CONTROLLER,
                Role.SYSTEM_ADMIN,
            }
            else Scope.OWN_FACILITY
        )

    all_facilities = list(
        db.scalars(
            select(Facility)
            .where(Facility.is_active.is_(True))
            .order_by(Facility.name_en)
        ).all()
    )
    scope_subject = CurrentUser(
        role=role,
        facility_id=active_facility.id if active_facility else None,
        facility_name=active_facility.name_en if active_facility else None,
        display_name=user.full_name,
        scope=selected_scope,
    )
    visible_ids = set(facility_ids_in_scope(scope_subject, all_facilities))
    scope_facilities = [facility for facility in all_facilities if facility.id in visible_ids]
    if not scope_facilities and active_facility is not None:
        scope_facilities = [active_facility]

    emergency_declared = False
    if role is Role.EMERGENCY_CONTROLLER:
        organization_ids = {
            facility.organization_id
            for facility in scope_facilities
            if facility.organization_id
        }
        if organization_ids:
            emergency_declared = bool(
                db.scalar(
                    select(func.count())
                    .select_from(EmergencyIncident)
                    .where(
                        EmergencyIncident.status == "ACTIVE",
                        EmergencyIncident.organization_id.in_(organization_ids),
                    )
                )
            )

    _slide_idle_window(db, session)

    return Principal(
        user=user,
        organization=organization,
        session=session,
        active_facility=active_facility,
        org_facilities=org_facilities,
        selected_scope=selected_scope,
        scope_facilities=scope_facilities,
        emergency_declared=emergency_declared,
    )


def require_principal(
    principal: Principal | None = Depends(optional_principal),
) -> Principal:
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in required",
        )

    # Accounts created or reset by an administrator receive a temporary
    # credential. They may sign out or reach the password screen, but no
    # operational dependency resolves until they replace it themselves.
    if principal.user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="Password change required",
            headers={"Location": "/account/password"},
        )

    return principal


def principal_can(principal: Principal, permission) -> bool:
    """Evaluate a domain permission for the authenticated web principal."""

    from app.auth import Role, can

    try:
        role = Role(principal.role)
    except ValueError:
        return False

    return can(
        principal.role_subject(role=role),
        permission,
    )


def require_permission(permission):
    """Route guard for a specific capability from the spec §13.1 matrix."""

    def guard(principal: Principal = Depends(require_principal)) -> Principal:
        if not principal_can(principal, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your role does not permit this action",
            )

        return principal

    return guard


def require_any_permission(*permissions):
    """Require at least one capability for a read or shared workspace.

    Write services still check their exact action. This dependency closes the
    equally important read boundary so hiding a navigation link is never the
    only thing protecting operational records.
    """

    def guard(principal: Principal = Depends(require_principal)) -> Principal:
        if not any(principal_can(principal, permission) for permission in permissions):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your role does not permit this workspace",
            )

        return principal

    return guard
