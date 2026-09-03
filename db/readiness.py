"""Database readiness checks used by startup, health probes, and release gates."""

from __future__ import annotations

from sqlalchemy import inspect, text

from db.base import Base
from db.session import engine

import db.models  # noqa: F401  (register model metadata)


def schema_drift() -> dict[str, list[str]]:
    """Report missing tables and columns without mutating the database."""

    inspector = inspect(engine)
    live_tables = set(inspector.get_table_names())
    missing_tables = sorted(set(Base.metadata.tables) - live_tables)
    missing_columns: list[str] = []

    for name, table in Base.metadata.tables.items():
        if name not in live_tables:
            continue

        live_columns = {column["name"] for column in inspector.get_columns(name)}
        missing_columns.extend(
            f"{name}.{column.name}"
            for column in table.columns
            if column.name not in live_columns
        )

    return {
        "missing_tables": missing_tables,
        "missing_columns": sorted(missing_columns),
    }


def readiness_report() -> tuple[bool, dict]:
    """Return a non-sensitive database and schema health summary."""

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        drift = schema_drift()
        ready = not drift["missing_tables"] and not drift["missing_columns"]

        return ready, {
            "database": "ok",
            "schema": "ok" if ready else "migration_required",
            "missing_tables": len(drift["missing_tables"]),
            "missing_columns": len(drift["missing_columns"]),
        }
    except Exception:
        return False, {
            "database": "unavailable",
            "schema": "unknown",
            "missing_tables": 0,
            "missing_columns": 0,
        }
