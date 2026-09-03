"""Audited, per-account onboarding state.

The guide is presentation state, but it still belongs to the signed-in account:
a person should not have to dismiss the same orientation on every browser. The
payload is versioned so a materially redesigned workflow can be introduced
without pretending an older orientation covered it.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from db.models import UserAccount
from services.audit import Actor, audited


ONBOARDING_VERSION = 1


def state(user: UserAccount) -> dict:
    """Return a stable state shape for old, null, or malformed preferences."""

    preferences = user.preferences_json if isinstance(user.preferences_json, dict) else {}
    saved = preferences.get("onboarding")

    if not isinstance(saved, dict):
        saved = {}

    complete = bool(
        saved.get("version") == ONBOARDING_VERSION
        and saved.get("completed_at")
    )

    return {
        "version": ONBOARDING_VERSION,
        "complete": complete,
        "completed_at": saved.get("completed_at") if complete else None,
    }


def _require_self(actor: Actor, user: UserAccount) -> None:
    if actor.user_id != user.id:
        raise ValueError("Onboarding state can only be changed by its own account")


def complete(db, actor: Actor, user: UserAccount) -> dict:
    """Remember that this account completed the current orientation."""

    _require_self(actor, user)
    before = state(user)
    preferences = deepcopy(user.preferences_json or {})
    completed_at = datetime.now(timezone.utc).isoformat()
    preferences["onboarding"] = {
        "version": ONBOARDING_VERSION,
        "completed_at": completed_at,
    }

    with audited(db, actor, "onboarding.completed", "user_account", user.id) as entry:
        user.preferences_json = preferences
        entry.on(user, before={"onboarding": before}, after={"onboarding": state(user)})

    return state(user)


def restart(db, actor: Actor, user: UserAccount) -> dict:
    """Make the first-run dashboard orientation visible again."""

    _require_self(actor, user)
    before = state(user)
    preferences = deepcopy(user.preferences_json or {})
    preferences.pop("onboarding", None)

    with audited(db, actor, "onboarding.restarted", "user_account", user.id) as entry:
        user.preferences_json = preferences
        entry.on(user, before={"onboarding": before}, after={"onboarding": state(user)})

    return state(user)
