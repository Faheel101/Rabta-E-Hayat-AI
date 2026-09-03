"""Role-aware, task-first guidance used by the dashboard and help workspace."""

from __future__ import annotations

from dataclasses import dataclass

from app.auth import Role
from i18n.t import t


@dataclass(frozen=True)
class Task:
    key: str
    url: str
    icon: str
    count_key: str | None = None


TASKS = {
    "donor_lookup": Task("donor_lookup", "/app/donors", "users"),
    "screen_donor": Task("screen_donor", "/app/donors", "shield"),
    "collection": Task("collection", "/app/sessions", "droplet"),
    "lab_queue": Task("lab_queue", "/app/lab", "flask", "lab_pending"),
    "processing": Task("processing", "/app/processing", "layers", "processing_pending"),
    "inventory": Task("inventory", "/app/inventory", "box"),
    "requests": Task("requests", "/app/requests", "clipboard", "requests_open"),
    "command": Task("command", "/insights/command-centre", "home"),
    "expiry": Task("expiry", "/insights/expiry-rescue", "clock"),
    "transfers": Task("transfers", "/insights/transfer-plan", "route", "transfers_pending"),
    "alerts": Task("alerts", "/insights/alerts", "inbox", "alerts_open"),
    "simulator": Task("simulator", "/insights/simulator", "alert"),
    "data": Task("data", "/data", "database", "data_issues"),
}


ROLE_TASKS = {
    Role.PHLEBOTOMIST: ("donor_lookup", "screen_donor", "collection"),
    Role.LAB_TECHNOLOGIST: ("lab_queue", "processing", "inventory"),
    Role.BLOOD_BANK_OFFICER: ("requests", "lab_queue", "expiry"),
    Role.RBC_COORDINATOR: ("command", "transfers", "data"),
    Role.PROVINCIAL_ADMIN: ("command", "alerts", "data"),
    Role.EMERGENCY_CONTROLLER: ("simulator", "alerts", "transfers"),
    Role.SYSTEM_ADMIN: ("data", "alerts", "command"),
}


ROLE_KEY = {
    Role.PHLEBOTOMIST: "phlebotomist",
    Role.LAB_TECHNOLOGIST: "lab_technologist",
    Role.BLOOD_BANK_OFFICER: "blood_bank_officer",
    Role.RBC_COORDINATOR: "rbc_coordinator",
    Role.PROVINCIAL_ADMIN: "provincial_admin",
    Role.EMERGENCY_CONTROLLER: "emergency_controller",
    Role.SYSTEM_ADMIN: "system_admin",
}


def greeting_name(display_name: str) -> str:
    """Keep a professional title with the given name in dashboard greetings."""

    parts = str(display_name or "").split()
    if not parts:
        return ""
    if len(parts) > 1 and parts[0].rstrip(".").lower() in {
        "dr",
        "mr",
        "mrs",
        "ms",
        "prof",
    }:
        return f"{parts[0]} {parts[1]}"
    return parts[0].rstrip(".")


def build_role_guide(
    role: str,
    counts: dict | None = None,
    *,
    language: str = "en",
) -> dict:
    """Translate the three useful next actions for the signed-in role."""

    try:
        role_enum = Role(role)
    except ValueError:
        role_enum = Role.BLOOD_BANK_OFFICER

    counts = counts or {}
    role_key = ROLE_KEY[role_enum]
    tasks = []

    for index, task_key in enumerate(ROLE_TASKS[role_enum], start=1):
        task = TASKS[task_key]
        count = counts.get(task.count_key) if task.count_key else None
        tasks.append(
            {
                "step": index,
                "key": task.key,
                "title": t(f"onboarding.tasks.{task.key}.title", language=language),
                "body": t(f"onboarding.tasks.{task.key}.body", language=language),
                "url": task.url,
                "icon": task.icon,
                "count": int(count or 0) if task.count_key else None,
            }
        )

    return {
        "role_key": role_key,
        "mission": t(f"onboarding.roles.{role_key}.mission", language=language),
        "handoff": t(f"onboarding.roles.{role_key}.handoff", language=language),
        "tasks": tasks,
        "first_action": tasks[0],
    }
