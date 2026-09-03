"""Navigation model.

Three rails, because the system has three tiers. Operations is the blood bank's
own daily work; Organization is across the group's facilities; Network is
collaboration with other organizations. Flattening them into one list is what
made the first attempt read as a single undifferentiated dashboard.

Visibility is by permission, not by hiding links that then 403 — a link a user
cannot use should not be drawn.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.auth import Permission, Role, can
from app.auth import CurrentUser as RoleSubject
from i18n.t import t


@dataclass
class NavItem:
    key: str
    label_key: str
    url: str
    icon: str
    permission: Permission | None = None
    # Roles that should never see this item even if they hold the permission.
    exclude_roles: tuple[str, ...] = ()
    count_key: str | None = None
    alert: bool = False


@dataclass
class NavSection:
    label_key: str
    items: list[NavItem] = field(default_factory=list)


# Operational rail — the vein-to-vein chain, in the order work actually flows.
OPERATIONS = NavSection(
    "nav.section_operations",
    [
        NavItem("dashboard", "nav.dashboard", "/app/dashboard", "home"),
        NavItem(
            "donors",
            "nav.donors",
            "/app/donors",
            "users",
            permission=Permission.REGISTER_DONOR,
        ),
        NavItem(
            "sessions",
            "nav.sessions",
            "/app/sessions",
            "droplet",
            permission=Permission.COLLECT_DONATION,
        ),
        # Only drawn for roles that hold SIGN_OFF_DEFERRAL. A phlebotomist
        # should not see a link they cannot use, and 609 donors currently sit
        # behind this queue.
        NavItem(
            "signoff",
            "nav.signoff",
            "/app/signoff",
            "shield",
            permission=Permission.SIGN_OFF_DEFERRAL,
            count_key="signoff_pending",
            alert=True,
        ),
        # Gated: a phlebotomist cannot run a plate or release anything, so the
        # bench is not their screen. Drawing the link anyway teaches people that
        # some nav items just fail.
        NavItem(
            "lab",
            "nav.lab",
            "/app/lab",
            "flask",
            permission=Permission.PERFORM_TEST,
            count_key="lab_pending",
            alert=True,
        ),
        NavItem(
            "processing",
            "nav.processing",
            "/app/processing",
            "layers",
            permission=Permission.PROCESS_COMPONENTS,
            count_key="processing_pending",
        ),
        NavItem(
            "inventory",
            "nav.inventory",
            "/app/inventory",
            "box",
            permission=Permission.VIEW_LOCAL_INVENTORY,
        ),
        NavItem(
            "requests",
            "nav.requests",
            "/app/requests",
            "clipboard",
            permission=Permission.MANAGE_CLINICAL_REQUEST,
            count_key="requests_open",
            alert=True,
        ),
        NavItem(
            "discards",
            "nav.discards",
            "/app/discards",
            "trash",
            permission=Permission.DISCARD_UNIT,
        ),
        NavItem(
            "storage",
            "nav.storage",
            "/app/storage",
            "thermometer",
            permission=Permission.VIEW_LOCAL_INVENTORY,
        ),
    ],
)

ORGANIZATION = NavSection(
    "nav.section_organization",
    [
        NavItem("org_overview", "nav.org_overview", "/org/overview", "building"),
        NavItem("org_facilities", "nav.org_facilities", "/org/facilities", "hospital"),
        NavItem("org_users", "nav.org_users", "/org/users", "shield",
                permission=Permission.MANAGE_USERS),
        NavItem("org_reports", "nav.org_reports", "/org/reports", "file"),
    ],
)

NETWORK = NavSection(
    "nav.section_network",
    [
        NavItem("net_availability", "nav.net_availability", "/network/availability", "search"),
        NavItem("net_requests", "nav.net_requests", "/network/requests", "inbox",
                count_key="network_requests_open"),
        NavItem("net_transfers", "nav.net_transfers", "/network/transfers", "truck",
                count_key="transfers_pending", alert=True),
        NavItem("net_map", "nav.net_map", "/network/map", "map"),
    ],
)

# Phase two. Registered now so the rail's shape is stable, gated so nothing
# half-built is reachable.
INSIGHTS = NavSection(
    "nav.section_insights",
    [
        NavItem(
            "command_centre",
            "nav.command_centre",
            "/insights/command-centre",
            "home",
            exclude_roles=("PHLEBOTOMIST", "LAB_TECHNOLOGIST"),
        ),
        NavItem(
            "forecast",
            "nav.forecast",
            "/insights/forecast",
            "trend",
            exclude_roles=("PHLEBOTOMIST", "LAB_TECHNOLOGIST"),
        ),
        NavItem(
            "expiry",
            "nav.expiry",
            "/insights/expiry-rescue",
            "clock",
            exclude_roles=("PHLEBOTOMIST", "LAB_TECHNOLOGIST"),
        ),
        NavItem(
            "plan",
            "nav.transfer_plan",
            "/insights/transfer-plan",
            "route",
            exclude_roles=("PHLEBOTOMIST", "LAB_TECHNOLOGIST"),
        ),
        NavItem(
            "simulator",
            "nav.simulator",
            "/insights/simulator",
            "alert",
            exclude_roles=("PHLEBOTOMIST", "LAB_TECHNOLOGIST"),
        ),
        NavItem(
            "alerts",
            "nav.alerts",
            "/insights/alerts",
            "inbox",
            exclude_roles=("PHLEBOTOMIST", "LAB_TECHNOLOGIST"),
            count_key="alerts_open",
            alert=True,
        ),
        NavItem(
            "facilities",
            "nav.facilities",
            "/insights/facilities",
            "hospital",
            exclude_roles=("PHLEBOTOMIST", "LAB_TECHNOLOGIST"),
        ),
        NavItem(
            "showcase",
            "nav.showcase",
            "/showcase",
            "play",
            exclude_roles=("PHLEBOTOMIST", "LAB_TECHNOLOGIST"),
        ),
        NavItem(
            "data",
            "nav.data",
            "/data",
            "database",
            permission=Permission.MANAGE_INTEGRATIONS,
            count_key="data_issues",
            alert=True,
        ),
        NavItem(
            "analytics",
            "nav.analytics",
            "/insights/analytics",
            "chart",
            exclude_roles=("PHLEBOTOMIST", "LAB_TECHNOLOGIST", "EMERGENCY_CONTROLLER"),
        ),
        NavItem(
            "admin",
            "nav.admin",
            "/admin",
            "shield",
            permission=Permission.MANAGE_USERS,
            exclude_roles=(
                "PHLEBOTOMIST",
                "LAB_TECHNOLOGIST",
                "BLOOD_BANK_OFFICER",
                "RBC_COORDINATOR",
                "EMERGENCY_CONTROLLER",
            ),
        ),
        NavItem(
            "ai_admin",
            "nav.ai_control",
            "/admin/ai",
            "sparkles",
            permission=Permission.VIEW_AUDIT_LOG,
            exclude_roles=(
                "PHLEBOTOMIST",
                "LAB_TECHNOLOGIST",
                "BLOOD_BANK_OFFICER",
                "RBC_COORDINATOR",
                "EMERGENCY_CONTROLLER",
            ),
        ),
    ],
)

SUPPORT = NavSection(
    "nav.section_support",
    [
        NavItem(
            "getting_started",
            "nav.getting_started",
            "/app/getting-started",
            "help",
        ),
    ],
)

SECTIONS = [OPERATIONS, ORGANIZATION, NETWORK, INSIGHTS, SUPPORT]


# The complete product remains available, but people should not have to scan the
# complete product to begin their shift. These three destinations are the stable
# front door for each role; everything else is progressively disclosed below.
# Dashboard is deliberately first for every role so switching roles never moves
# the user's home anchor.
ROLE_FOCUS_KEYS: dict[Role, tuple[str, ...]] = {
    Role.PHLEBOTOMIST: ("dashboard", "donors", "sessions"),
    Role.LAB_TECHNOLOGIST: ("dashboard", "lab", "processing"),
    Role.BLOOD_BANK_OFFICER: ("dashboard", "requests", "inventory"),
    Role.RBC_COORDINATOR: ("dashboard", "command_centre", "plan"),
    Role.PROVINCIAL_ADMIN: ("dashboard", "command_centre", "alerts"),
    Role.EMERGENCY_CONTROLLER: ("dashboard", "simulator", "alerts"),
    Role.SYSTEM_ADMIN: ("dashboard", "data", "admin"),
}


def build_nav(
    *,
    role: str,
    current_path: str,
    counts: dict | None = None,
    enabled_keys: set[str] | None = None,
    language: str | None = None,
) -> list[dict]:
    """Build the rails for this user.

    `language` has to be threaded through explicitly. t() defaults to English
    when no language is given, so omitting it rendered the entire sidebar in
    English for Urdu users while ur.json sat there holding every translation —
    a bug that looks exactly like missing translations and is not.
    """
    counts = counts or {}

    try:
        role_enum = Role(role)
    except ValueError:
        role_enum = Role.BLOOD_BANK_OFFICER

    subject = RoleSubject(
        role=role_enum,
        facility_id=None,
        facility_name=None,
        display_name="",
    )

    rendered = []
    visible_by_key: dict[str, dict] = {}

    for section in SECTIONS:
        items = []

        for item in section.items:
            if enabled_keys is not None and item.key not in enabled_keys:
                continue

            if item.permission is not None and not can(subject, item.permission):
                continue

            if role in item.exclude_roles:
                continue

            count = counts.get(item.count_key) if item.count_key else None

            rendered_item = {
                "key": item.key,
                "label": t(item.label_key, language=language),
                "url": item.url,
                "icon": item.icon,
                # Longest-prefix match, so /app/donors/new keeps Donors lit.
                "active": current_path == item.url
                or current_path.startswith(item.url + "/"),
                "count": count if count else None,
                "alert": item.alert and bool(count),
            }
            items.append(rendered_item)
            visible_by_key[item.key] = rendered_item

        if items:
            rendered.append(
                {
                    "label": t(section.label_key, language=language),
                    "entries": items,
                    "focus": False,
                    "active": any(item["active"] for item in items),
                    "attention": sum(1 for item in items if item["alert"]),
                }
            )

    focus_keys = ROLE_FOCUS_KEYS.get(role_enum, ("dashboard",))
    focus_entries = [visible_by_key[key] for key in focus_keys if key in visible_by_key]
    focus_key_set = {item["key"] for item in focus_entries}

    # Do not repeat a focus destination inside a collapsed section. Repetition
    # looks like extra capability and makes users wonder which link is canonical.
    secondary = []

    for section in rendered:
        entries = [item for item in section["entries"] if item["key"] not in focus_key_set]

        if not entries:
            continue

        secondary.append(
            {
                **section,
                "entries": entries,
                "active": any(item["active"] for item in entries),
                "attention": sum(1 for item in entries if item["alert"]),
            }
        )

    if focus_entries:
        return [
            {
                "label": t("nav.section_my_work", language=language),
                "entries": focus_entries,
                "focus": True,
                "active": any(item["active"] for item in focus_entries),
                "attention": sum(1 for item in focus_entries if item["alert"]),
            },
            *secondary,
        ]

    return secondary
