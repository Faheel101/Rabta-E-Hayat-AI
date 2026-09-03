"""Roles, permissions and facility scope (spec §13.1).

Every check here is real. The active role comes from the authenticated,
tenant-scoped UserAccount resolved by the server-side session. Services,
routers, navigation, and role guidance reuse this matrix so a hidden control
and a direct URL enforce the same boundary.

Spec §2.2 is explicit that this system never moves blood without human approval,
so the approval permissions below are the load-bearing part: they decide who may
sign off a transfer, and every exercise of them writes to audit_log.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from i18n.t import t


class Role(str, Enum):
    # Bench roles. Real blood banks segregate duties: the person who bleeds a
    # donor is not the person who releases the lab result, and release itself
    # needs a second pair of eyes. Without distinct roles the audit trail records
    # a name but proves nothing.
    PHLEBOTOMIST = "PHLEBOTOMIST"
    LAB_TECHNOLOGIST = "LAB_TECHNOLOGIST"

    BLOOD_BANK_OFFICER = "BLOOD_BANK_OFFICER"
    RBC_COORDINATOR = "RBC_COORDINATOR"
    PROVINCIAL_ADMIN = "PROVINCIAL_ADMIN"
    EMERGENCY_CONTROLLER = "EMERGENCY_CONTROLLER"
    SYSTEM_ADMIN = "SYSTEM_ADMIN"


ROLE_LABEL_KEYS = {
    Role.PHLEBOTOMIST: "role.phlebotomist",
    Role.LAB_TECHNOLOGIST: "role.lab_technologist",
    Role.BLOOD_BANK_OFFICER: "role.bbo",
    Role.RBC_COORDINATOR: "role.rbc_coordinator",
    Role.PROVINCIAL_ADMIN: "role.provincial_admin",
    Role.EMERGENCY_CONTROLLER: "role.emergency_controller",
    Role.SYSTEM_ADMIN: "role.system_admin",
}


ORG_TYPE_LABEL_KEYS = {
    "HOSPITAL_GROUP": "org_type.hospital_group",
    "STANDALONE_HOSPITAL": "org_type.standalone",
    "RBC_OPERATOR": "org_type.rbc_operator",
    "GOVT_PROGRAMME": "org_type.govt_programme",
}


class Scope(str, Enum):
    """How wide a role can see. Ordered from narrowest to widest."""

    OWN_FACILITY = "OWN_FACILITY"
    PARENT_RBC = "PARENT_RBC"
    RBC_NETWORK = "RBC_NETWORK"
    DISTRICT = "DISTRICT"
    DIVISION = "DIVISION"
    PROVINCE = "PROVINCE"
    ALL = "ALL"


SCOPE_ORDER = [
    Scope.OWN_FACILITY,
    Scope.PARENT_RBC,
    Scope.RBC_NETWORK,
    Scope.DISTRICT,
    Scope.DIVISION,
    Scope.PROVINCE,
    Scope.ALL,
]

SCOPE_LABEL_KEYS = {
    Scope.OWN_FACILITY: "scope.my_facility",
    Scope.PARENT_RBC: "scope.my_rbc",
    Scope.RBC_NETWORK: "scope.my_rbc",
    Scope.DISTRICT: "scope.district",
    Scope.DIVISION: "scope.division",
    Scope.PROVINCE: "scope.province",
    Scope.ALL: "scope.all",
}


class Permission(str, Enum):
    # Operational permissions, one per step of the vein-to-vein chain.
    REGISTER_DONOR = "REGISTER_DONOR"
    SCREEN_DONOR = "SCREEN_DONOR"
    COLLECT_DONATION = "COLLECT_DONATION"
    PERFORM_TEST = "PERFORM_TEST"
    # Deliberately separate from PERFORM_TEST: release is the control point where
    # a unit becomes issuable, and it must be a second person.
    VERIFY_TEST_RELEASE = "VERIFY_TEST_RELEASE"
    PROCESS_COMPONENTS = "PROCESS_COMPONENTS"
    # Reading patient-ready local stock is operational access in its own right.
    # It must not be implied by VIEW_NETWORK: phlebotomists and emergency
    # controllers may see safe aggregate signals, but not unit-level bench data.
    VIEW_LOCAL_INVENTORY = "VIEW_LOCAL_INVENTORY"
    MANAGE_CLINICAL_REQUEST = "MANAGE_CLINICAL_REQUEST"
    PERFORM_CROSSMATCH = "PERFORM_CROSSMATCH"
    ISSUE_UNIT = "ISSUE_UNIT"
    RECORD_TRANSFUSION = "RECORD_TRANSFUSION"
    DISCARD_UNIT = "DISCARD_UNIT"
    # Resolving a deferral that rests on one of the 7 rules the sources
    # disagree about. Its own permission rather than EDIT_REFERENCE_DATA:
    # this is a clinical judgement about one donor, not a change to a
    # reference table, and the people who should hold it are not the same.
    SIGN_OFF_DEFERRAL = "SIGN_OFF_DEFERRAL"

    VIEW_NETWORK = "VIEW_NETWORK"
    APPROVE_TRANSFER_OUT = "APPROVE_TRANSFER_OUT"
    ACCEPT_TRANSFER_IN = "ACCEPT_TRANSFER_IN"
    RUN_OPTIMIZER = "RUN_OPTIMIZER"
    CHANGE_OPTIMIZER_WEIGHTS = "CHANGE_OPTIMIZER_WEIGHTS"
    RUN_SIMULATION = "RUN_SIMULATION"
    DECLARE_EMERGENCY = "DECLARE_EMERGENCY"
    ACKNOWLEDGE_ALERT = "ACKNOWLEDGE_ALERT"
    RESOLVE_ALERT = "RESOLVE_ALERT"
    MANAGE_INTEGRATIONS = "MANAGE_INTEGRATIONS"
    # Creating a tenant or activating a new facility changes the platform's
    # security boundary. It is intentionally narrower than editing settings on
    # an existing facility or managing users inside one organization.
    MANAGE_NETWORK = "MANAGE_NETWORK"
    EDIT_FACILITY_SETTINGS = "EDIT_FACILITY_SETTINGS"
    EDIT_REFERENCE_DATA = "EDIT_REFERENCE_DATA"
    MANAGE_USERS = "MANAGE_USERS"
    VIEW_AUDIT_LOG = "VIEW_AUDIT_LOG"


# Spec §13.1, transcribed. A blood bank officer may view prepared simulation
# results but may not run one or declare a live emergency — a drill and an
# incident must never be confusable (§12.8).
ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    # Bleeds donors. Cannot test, cannot release, cannot issue — which is the
    # point of having the role at all.
    Role.PHLEBOTOMIST: {
        Permission.VIEW_NETWORK,
        Permission.REGISTER_DONOR,
        Permission.SCREEN_DONOR,
        Permission.COLLECT_DONATION,
    },
    # Runs the panel and releases. Never touches collection, so a reactive result
    # cannot be quietly buried by the person who took the bag.
    Role.LAB_TECHNOLOGIST: {
        Permission.VIEW_NETWORK,
        Permission.PERFORM_TEST,
        Permission.VERIFY_TEST_RELEASE,
        Permission.PROCESS_COMPONENTS,
        Permission.VIEW_LOCAL_INVENTORY,
        Permission.DISCARD_UNIT,
    },
    Role.BLOOD_BANK_OFFICER: {
        Permission.VIEW_NETWORK,
        Permission.SIGN_OFF_DEFERRAL,
        Permission.REGISTER_DONOR,
        Permission.SCREEN_DONOR,
        Permission.COLLECT_DONATION,
        Permission.PERFORM_TEST,
        Permission.VERIFY_TEST_RELEASE,
        Permission.PROCESS_COMPONENTS,
        Permission.VIEW_LOCAL_INVENTORY,
        Permission.MANAGE_CLINICAL_REQUEST,
        Permission.PERFORM_CROSSMATCH,
        Permission.ISSUE_UNIT,
        Permission.RECORD_TRANSFUSION,
        Permission.DISCARD_UNIT,
        Permission.APPROVE_TRANSFER_OUT,
        Permission.ACCEPT_TRANSFER_IN,
        Permission.RUN_OPTIMIZER,
        Permission.ACKNOWLEDGE_ALERT,
        Permission.RESOLVE_ALERT,
        Permission.EDIT_FACILITY_SETTINGS,
        Permission.VIEW_AUDIT_LOG,
    },
    Role.RBC_COORDINATOR: {
        Permission.VIEW_NETWORK,
        Permission.SIGN_OFF_DEFERRAL,
        Permission.REGISTER_DONOR,
        Permission.SCREEN_DONOR,
        Permission.COLLECT_DONATION,
        Permission.PERFORM_TEST,
        Permission.VERIFY_TEST_RELEASE,
        Permission.PROCESS_COMPONENTS,
        Permission.VIEW_LOCAL_INVENTORY,
        Permission.MANAGE_CLINICAL_REQUEST,
        Permission.PERFORM_CROSSMATCH,
        Permission.ISSUE_UNIT,
        Permission.RECORD_TRANSFUSION,
        Permission.DISCARD_UNIT,
        Permission.APPROVE_TRANSFER_OUT,
        Permission.ACCEPT_TRANSFER_IN,
        Permission.RUN_OPTIMIZER,
        Permission.CHANGE_OPTIMIZER_WEIGHTS,
        Permission.RUN_SIMULATION,
        Permission.DECLARE_EMERGENCY,
        Permission.ACKNOWLEDGE_ALERT,
        Permission.RESOLVE_ALERT,
        Permission.MANAGE_INTEGRATIONS,
        Permission.EDIT_FACILITY_SETTINGS,
        Permission.MANAGE_USERS,
        Permission.VIEW_AUDIT_LOG,
    },
    Role.PROVINCIAL_ADMIN: {
        Permission.VIEW_NETWORK,
        Permission.SIGN_OFF_DEFERRAL,
        Permission.REGISTER_DONOR,
        Permission.SCREEN_DONOR,
        Permission.COLLECT_DONATION,
        Permission.PERFORM_TEST,
        Permission.VERIFY_TEST_RELEASE,
        Permission.PROCESS_COMPONENTS,
        Permission.VIEW_LOCAL_INVENTORY,
        Permission.MANAGE_CLINICAL_REQUEST,
        Permission.PERFORM_CROSSMATCH,
        Permission.ISSUE_UNIT,
        Permission.RECORD_TRANSFUSION,
        Permission.DISCARD_UNIT,
        Permission.APPROVE_TRANSFER_OUT,
        Permission.ACCEPT_TRANSFER_IN,
        Permission.RUN_OPTIMIZER,
        Permission.CHANGE_OPTIMIZER_WEIGHTS,
        Permission.RUN_SIMULATION,
        Permission.DECLARE_EMERGENCY,
        Permission.ACKNOWLEDGE_ALERT,
        Permission.RESOLVE_ALERT,
        Permission.MANAGE_INTEGRATIONS,
        Permission.EDIT_FACILITY_SETTINGS,
        Permission.MANAGE_USERS,
        Permission.VIEW_AUDIT_LOG,
    },
    Role.EMERGENCY_CONTROLLER: {
        Permission.VIEW_NETWORK,
        # "emergency only" in the matrix: approval is granted while an emergency
        # is declared, which is enforced in `can()` rather than assumed here.
        Permission.ACCEPT_TRANSFER_IN,
        Permission.RUN_OPTIMIZER,
        Permission.RUN_SIMULATION,
        Permission.DECLARE_EMERGENCY,
        Permission.ACKNOWLEDGE_ALERT,
        Permission.RESOLVE_ALERT,
        Permission.VIEW_AUDIT_LOG,
    },
    # Platform administration is deliberately not a clinical super-user. A
    # system administrator can operate the platform and planning engine, but a
    # temporary, audited clinical assignment is required to touch a donor, bag,
    # test, issue or transfusion.
    Role.SYSTEM_ADMIN: {
        Permission.VIEW_NETWORK,
        Permission.RUN_OPTIMIZER,
        Permission.CHANGE_OPTIMIZER_WEIGHTS,
        Permission.RUN_SIMULATION,
        Permission.ACKNOWLEDGE_ALERT,
        Permission.RESOLVE_ALERT,
        Permission.MANAGE_INTEGRATIONS,
        Permission.MANAGE_NETWORK,
        Permission.EDIT_FACILITY_SETTINGS,
        Permission.EDIT_REFERENCE_DATA,
        Permission.MANAGE_USERS,
        Permission.VIEW_AUDIT_LOG,
    },
}

# Widest scope each role may view (spec §13.1 "View network inventory").
ROLE_MAX_SCOPE: dict[Role, Scope] = {
    # Bench staff work at one bench. They have no business browsing the
    # network's inventory.
    Role.PHLEBOTOMIST: Scope.OWN_FACILITY,
    Role.LAB_TECHNOLOGIST: Scope.OWN_FACILITY,
    Role.BLOOD_BANK_OFFICER: Scope.PARENT_RBC,
    Role.RBC_COORDINATOR: Scope.RBC_NETWORK,
    Role.PROVINCIAL_ADMIN: Scope.PROVINCE,
    Role.EMERGENCY_CONTROLLER: Scope.PROVINCE,
    Role.SYSTEM_ADMIN: Scope.ALL,
}

# Decision-intelligence page keys a role may open. Operational workspaces use
# the more granular permissions above because collection, testing, inventory,
# request handling, and issue are independently segregated duties.
ROLE_PAGES: dict[Role, set[str]] = {
    Role.PHLEBOTOMIST: {
        "dashboard",
        "donors",
        "sessions",
    },
    Role.LAB_TECHNOLOGIST: {
        "dashboard",
        "lab",
        "processing",
        "inventory",
        "discards",
        "storage",
    },
    Role.BLOOD_BANK_OFFICER: {
        "command_centre",
        "forecast",
        "expiry",
        "transfers",
        "simulator",
        "alerts",
        "facilities",
        "analytics",
    },
    Role.RBC_COORDINATOR: {
        "command_centre",
        "forecast",
        "expiry",
        "transfers",
        "simulator",
        "alerts",
        "facilities",
        "analytics",
        "data",
    },
    Role.PROVINCIAL_ADMIN: {
        "command_centre",
        "forecast",
        "expiry",
        "transfers",
        "simulator",
        "alerts",
        "facilities",
        "analytics",
        "data",
        "admin",
    },
    Role.EMERGENCY_CONTROLLER: {
        "command_centre",
        "forecast",
        "expiry",
        "transfers",
        "simulator",
        "alerts",
        "facilities",
    },
    Role.SYSTEM_ADMIN: {
        "command_centre",
        "forecast",
        "expiry",
        "transfers",
        "simulator",
        "alerts",
        "facilities",
        "analytics",
        "data",
        "admin",
    },
}


@dataclass(frozen=True)
class CurrentUser:
    role: Role
    facility_id: str | None
    facility_name: str | None
    display_name: str
    scope: Scope = Scope.OWN_FACILITY
    emergency_declared: bool = False

    @property
    def role_label(self) -> str:
        return t(ROLE_LABEL_KEYS[self.role])

    def with_scope(self, scope: Scope) -> "CurrentUser":
        return replace(self, scope=scope)


def can(user: CurrentUser, permission: Permission) -> bool:
    try:
        role = Role(user.role)
    except ValueError:
        return False
    granted = ROLE_PERMISSIONS.get(role, set())

    if permission not in granted:
        # The emergency controller's approval right is conditional, not absent.
        if (
            permission is Permission.APPROVE_TRANSFER_OUT
            and role is Role.EMERGENCY_CONTROLLER
        ):
            return user.emergency_declared

        return False

    return True


def can_open_page(user: CurrentUser, page_key: str) -> bool:
    try:
        role = Role(user.role)
    except ValueError:
        return False
    return page_key in ROLE_PAGES.get(role, set())


def allowed_scopes(user: CurrentUser) -> list[Scope]:
    """Scopes this role may select, narrowest first."""

    ceiling = ROLE_MAX_SCOPE.get(user.role, Scope.OWN_FACILITY)
    limit = SCOPE_ORDER.index(ceiling)

    # Collapse the duplicate labels (PARENT_RBC and RBC_NETWORK both read as
    # "My RBC network") so the selector does not show the same option twice.
    seen = set()
    scopes = []

    for scope in SCOPE_ORDER[: limit + 1]:
        label = SCOPE_LABEL_KEYS[scope]

        if label in seen:
            continue

        seen.add(label)
        scopes.append(scope)

    return scopes


def facility_ids_in_scope(user: CurrentUser, facilities) -> list[str]:
    """Which facilities this user may see, given their scope selection.

    `facilities` is a list of dicts or records carrying facility_id,
    parent_rbc_id, district and division.
    """

    def value(record, name):
        if isinstance(record, dict):
            return record.get(name, record.get("id") if name == "facility_id" else None)

        result = getattr(record, name, None)
        if result is None and name == "facility_id":
            result = getattr(record, "id", None)
        return result

    own = next(
        (
            record
            for record in facilities
            if value(record, "facility_id") == user.facility_id
        ),
        None,
    )

    if user.scope is Scope.ALL:
        return [value(record, "facility_id") for record in facilities]

    if own is None:
        return [user.facility_id] if user.facility_id else []

    if user.scope is Scope.OWN_FACILITY:
        return [user.facility_id]

    if user.scope in (Scope.PARENT_RBC, Scope.RBC_NETWORK):
        hub_id = value(own, "parent_rbc_id") or user.facility_id

        return [
            value(record, "facility_id")
            for record in facilities
            if value(record, "parent_rbc_id") == hub_id
            or value(record, "facility_id") == hub_id
        ]

    if user.scope is Scope.DISTRICT:
        district = value(own, "district")

        return [
            value(record, "facility_id")
            for record in facilities
            if value(record, "district") == district
        ]

    if user.scope is Scope.DIVISION:
        division = value(own, "division")

        return [
            value(record, "facility_id")
            for record in facilities
            if value(record, "division") == division
        ]

    if user.scope is Scope.PROVINCE:
        province = value(own, "province")

        return [
            value(record, "facility_id")
            for record in facilities
            if value(record, "province") == province
        ]

    return [user.facility_id]


DEFAULT_ROLE = Role.BLOOD_BANK_OFFICER
