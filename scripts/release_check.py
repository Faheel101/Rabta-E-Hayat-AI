"""Fast, non-mutating release readiness gate for startup and operators."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from sqlalchemy import func, select

from config.settings import (
    APP_ENV,
    APP_NAME,
    APP_VERSION,
    BASE_DIR,
    DATABASE_URL,
    validate_runtime_config,
)
from db.models import BloodUnit, Facility, UserAccount
from db.readiness import readiness_report
from db.session import IS_SQLITE, SessionLocal
from scripts.backup_restore import sqlite_path


REQUIRED_ASSETS = (
    "web/static/css/app.css",
    "web/static/vendor/alpine.min.js",
    "web/static/vendor/htmx.min.js",
    "web/static/favicon.svg",
)


def run_checks() -> tuple[bool, dict]:
    checks: dict[str, object] = {}

    try:
        validate_runtime_config()
        checks["configuration"] = "ok"
    except RuntimeError as error:
        checks["configuration"] = str(error)

    ready, database = readiness_report()
    checks["database"] = database

    missing_assets = [name for name in REQUIRED_ASSETS if not (BASE_DIR / name).is_file()]
    checks["assets"] = "ok" if not missing_assets else {"missing": missing_assets}

    integrity = "not_applicable"

    if IS_SQLITE:
        try:
            connection = sqlite3.connect(
                f"file:{sqlite_path(DATABASE_URL).as_posix()}?mode=ro", uri=True
            )
            try:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                connection.close()
        except Exception:
            integrity = "failed"

    checks["integrity"] = integrity

    if APP_ENV == "demo" and ready:
        db = SessionLocal()
        try:
            counts = {
                "active_users": db.scalar(
                    select(func.count()).select_from(UserAccount).where(UserAccount.is_active.is_(True))
                ) or 0,
                "active_facilities": db.scalar(
                    select(func.count()).select_from(Facility).where(Facility.is_active.is_(True))
                ) or 0,
                "blood_units": db.scalar(select(func.count()).select_from(BloodUnit)) or 0,
            }
        finally:
            db.close()

        checks["demo_data"] = counts
    else:
        counts = None

    ok = (
        checks["configuration"] == "ok"
        and ready
        and checks["assets"] == "ok"
        and integrity in {"ok", "not_applicable"}
        and (
            counts is None
            or all(int(value) > 0 for value in counts.values())
        )
    )

    return ok, {
        "status": "ready" if ok else "not_ready",
        "service": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check release readiness.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    ok, report = run_checks()

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{report['service']} {report['version']}: {report['status']}")
        for name, result in report["checks"].items():
            print(f"  {name}: {result}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
