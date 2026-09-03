"""The audited unit of work: every state change and its trail, together.

`audit_log` has existed since the schema was written and nothing has ever written
to it. That is worse than not having it — a system with an empty audit table
looks accountable and is not.

The rule this module enforces is that a change and its audit entry share a
transaction. Not "the route remembers to log"; not a decorator that a new caller
can forget. If the audit write fails the domain change rolls back with it, and a
change cannot exist without a record of who made it.

    with audited(db, actor, "DONOR_SCREENED", "donor_screening") as entry:
        screening = DonorScreening(...)
        db.add(screening)
        db.flush()
        entry.on(screening, after=snapshot(screening, SCREENING_FIELDS))

Every service function in this package takes an `Actor` as its first argument
rather than reading one from a request. A service that can look up "the current
user" is a service that can be called with nobody responsible for the call.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy.orm import Session

from db.models import AuditLog


@dataclass(frozen=True)
class Actor:
    """Who is making a change, carried explicitly.

    Passed in rather than looked up, so a service function cannot be invoked
    without somebody accountable for it. `ip` is optional because background
    jobs have no request behind them — those record the job name as the actor.
    """

    user_id: str
    display_name: str
    role: str
    facility_id: str | None = None
    organization_id: str | None = None
    organization_wide: bool = False
    scope_facility_ids: tuple[str, ...] = ()
    emergency_declared: bool = False
    ip: str | None = None

    @classmethod
    def from_principal(cls, principal, request=None) -> "Actor":
        client = getattr(request, "client", None) if request is not None else None

        return cls(
            user_id=principal.user_id,
            display_name=principal.display_name,
            role=principal.role,
            facility_id=principal.facility_id,
            organization_id=principal.organization_id,
            organization_wide=bool(getattr(principal, "is_group_user", False)),
            scope_facility_ids=tuple(getattr(principal, "scope_facility_ids", ()) or ()),
            emergency_declared=bool(getattr(principal, "emergency_declared", False)),
            ip=getattr(client, "host", None),
        )

    @classmethod
    def system(cls, job: str) -> "Actor":
        """A pipeline or scheduled job. Named, so the trail never reads 'None'."""

        return cls(
            user_id=f"system:{job}",
            display_name=f"System ({job})",
            role="SYSTEM",
        )


def _plain(value: Any) -> Any:
    """Make a value safe to store as JSON without losing what it meant."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]

    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}

    return str(value)


def snapshot(instance, fields: Iterable[str]) -> dict:
    """The named fields of a record, as plain JSON values.

    Fields are named explicitly rather than taken from the whole row. A blanket
    snapshot would copy donor identity into a second table, which is a privacy
    problem as much as a size one — the audit trail should say what changed, not
    duplicate the record.
    """

    return {name: _plain(getattr(instance, name, None)) for name in fields}


def changed_fields(before: dict | None, after: dict | None) -> dict:
    """Only what actually moved, as {field: {"from": x, "to": y}}.

    Storing an unchanged field alongside a changed one buries the answer to the
    question the trail exists for.
    """

    before = before or {}
    after = after or {}

    return {
        key: {"from": before.get(key), "to": after.get(key)}
        for key in set(before) | set(after)
        if before.get(key) != after.get(key)
    }


class AuditEntry:
    """A pending audit record, filled in as the change is made."""

    def __init__(self, action: str, entity_type: str, entity_id: str | None = None):
        self.action = action
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.before: dict | None = None
        self.after: dict | None = None
        self.context: dict = {}

    def on(
        self,
        instance=None,
        *,
        before: dict | None = None,
        after: dict | None = None,
        entity_id: str | None = None,
    ) -> "AuditEntry":
        """Attach the record this entry is about, and what changed on it."""

        if instance is not None and entity_id is None:
            entity_id = getattr(instance, "id", None)

        if entity_id is not None:
            self.entity_id = entity_id

        if before is not None:
            self.before = before

        if after is not None:
            self.after = after

        return self

    def note(self, **values) -> "AuditEntry":
        """Context that is not a field change — a reason, an override, a count."""

        self.context.update({key: _plain(value) for key, value in values.items()})

        return self


@contextmanager
def audited(
    db: Session,
    actor: Actor,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
):
    """Run a state change and write its audit entry in one transaction.

    The commit happens here, once, after the audit row is added. A caller that
    commits inside the block breaks that guarantee, which is why no service
    function in this package commits.

    On any exception the whole thing rolls back — including the audit row, since
    a change that did not happen must not leave a record saying it did.
    """

    entry = AuditEntry(action, entity_type, entity_id)

    try:
        yield entry

        changes = changed_fields(entry.before, entry.after)

        payload_after = dict(entry.after or {})

        if entry.context:
            payload_after["_context"] = entry.context

        if changes:
            payload_after["_changed"] = changes

        db.add(
            AuditLog(
                id=str(uuid.uuid4()),
                # Real time, deliberately, while domain records use the frozen
                # demo instant. An audit entry records when a person acted; a
                # trail where every row shares a timestamp cannot be ordered,
                # and ordering is most of what it is for.
                created_at=datetime.now(timezone.utc),
                actor=f"{actor.display_name} <{actor.user_id}>",
                action=action,
                entity_type=entity_type,
                entity_id=entry.entity_id,
                before_json=entry.before,
                after_json=payload_after or None,
                actor_ip=actor.ip,
            )
        )

        # Keep the invalidation marker in the same transaction as the domain
        # change and its audit evidence.  A committed clinical mutation can
        # therefore never be invisible to the refresh worker, even if the
        # process stops immediately after this commit.
        from services.intelligence_refresh import (
            affects_decision_intelligence,
            mark_dirty_in_transaction,
        )

        if affects_decision_intelligence(action, entity_type):
            mark_dirty_in_transaction(
                db,
                action=action,
                requested_by=f"{actor.display_name} <{actor.user_id}>",
            )

        db.commit()

    except Exception:
        db.rollback()
        raise


class ServiceError(Exception):
    """A refusal the caller should show the user, not a crash.

    Carries a stable `code` so a route can map it to a message and a field
    without matching on prose.
    """

    def __init__(self, code: str, message: str, *, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


class PermissionDenied(ServiceError):
    """The actor holds no permission for this action.

    Raised by the service, not checked in the route, so a second caller of the
    same function cannot bypass it.
    """

    def __init__(self, permission, action: str):
        super().__init__(
            "PERMISSION_DENIED",
            f"This role may not {action}.",
        )
        self.permission = permission


def require(actor: Actor, permission, action: str) -> None:
    """Guard a service function on a permission from the §13.1 matrix.

    Enforced here rather than at the route because a route guard protects one
    entry point, and a service function will eventually have several.
    """

    from app.auth import CurrentUser, can

    subject = CurrentUser(
        role=actor.role,
        facility_id=actor.facility_id,
        facility_name=None,
        display_name=actor.display_name,
        emergency_declared=actor.emergency_declared,
    )

    if not can(subject, permission):
        raise PermissionDenied(permission, action)
