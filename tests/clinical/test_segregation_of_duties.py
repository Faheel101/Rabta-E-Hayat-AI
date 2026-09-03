"""Segregation of duties across the vein-to-vein chain.

A blood bank's audit trail is only worth something if the roles it records are
actually distinct. These tests pin the separations that matter clinically: the
person who bleeds a donor must not be the person who releases the lab result,
and no role may both test and be the sole signature on release.
"""

from __future__ import annotations

import pytest

from app.auth import (
    ROLE_MAX_SCOPE,
    ROLE_PAGES,
    ROLE_PERMISSIONS,
    CurrentUser,
    Permission,
    Role,
    Scope,
    can,
)

BENCH_ROLES = [Role.PHLEBOTOMIST, Role.LAB_TECHNOLOGIST]


def user(role: Role) -> CurrentUser:
    return CurrentUser(
        role=role, facility_id="F-TEST", facility_name="Test", display_name="Test"
    )


def test_phlebotomist_cannot_touch_the_lab():
    """Whoever took the bag must not be able to sign off its own screening."""

    subject = user(Role.PHLEBOTOMIST)

    assert can(subject, Permission.COLLECT_DONATION)
    assert not can(subject, Permission.PERFORM_TEST)
    assert not can(subject, Permission.VERIFY_TEST_RELEASE)
    assert not can(subject, Permission.ISSUE_UNIT)


def test_lab_technologist_cannot_collect():
    """The reverse direction. A reactive result must not be buryable by the
    person who would have to explain it."""

    subject = user(Role.LAB_TECHNOLOGIST)

    assert can(subject, Permission.PERFORM_TEST)
    assert can(subject, Permission.VERIFY_TEST_RELEASE)
    assert not can(subject, Permission.COLLECT_DONATION)
    assert not can(subject, Permission.SCREEN_DONOR)


def test_lab_technologist_cannot_issue_to_a_patient():
    """Release makes a unit issuable; issuing it to a named patient is a
    separate decision made by a separate person."""

    assert not can(user(Role.LAB_TECHNOLOGIST), Permission.ISSUE_UNIT)
    assert not can(user(Role.LAB_TECHNOLOGIST), Permission.PERFORM_CROSSMATCH)


@pytest.mark.parametrize("role", BENCH_ROLES)
def test_bench_roles_are_confined_to_their_own_facility(role):
    """A phlebotomist has no reason to browse another hospital's stock."""

    assert ROLE_MAX_SCOPE[role] is Scope.OWN_FACILITY


@pytest.mark.parametrize("role", BENCH_ROLES)
def test_bench_roles_cannot_approve_movements_or_administer(role):
    forbidden = {
        Permission.APPROVE_TRANSFER_OUT,
        Permission.ACCEPT_TRANSFER_IN,
        Permission.DECLARE_EMERGENCY,
        Permission.MANAGE_USERS,
        Permission.EDIT_REFERENCE_DATA,
        Permission.CHANGE_OPTIMIZER_WEIGHTS,
    }

    granted = ROLE_PERMISSIONS[role] & forbidden

    assert not granted, f"{role.value} should not hold {[p.value for p in granted]}"


@pytest.mark.parametrize("role", BENCH_ROLES)
def test_bench_roles_only_reach_pages_they_work_in(role):
    pages = ROLE_PAGES[role]

    assert pages, f"{role.value} has no pages and could not log in usefully"
    assert "network" not in pages
    assert "admin" not in pages


def test_no_permission_is_orphaned():
    """Every permission must be reachable by some role; an unreachable one is a
    dead check that silently blocks a real workflow."""

    granted = set().union(*ROLE_PERMISSIONS.values())
    orphans = set(Permission) - granted

    assert not orphans, f"no role can exercise {[p.value for p in orphans]}"


def test_every_role_has_a_scope_and_pages():
    for role in Role:
        assert role in ROLE_MAX_SCOPE, f"{role.value} has no scope ceiling"
        assert ROLE_PAGES.get(role), f"{role.value} has no pages"
