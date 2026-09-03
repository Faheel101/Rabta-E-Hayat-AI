"""Canonical integration-feed health used by every web surface.

An adapter's operational state takes precedence over a mart snapshot. The mart
is only the fallback for facilities that have not yet created an adapter row.
Keeping this rule here prevents the permanent footer, Command Centre and Data
workspace from disagreeing about whether a facility is healthy.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.clock import as_utc
from db.models import Facility, IntegrationFeed, MartFacilityKpi


HEALTHY = {"HEALTHY"}
DEGRADED = {"DEGRADED", "STALE", "OFFLINE", "NEVER_SYNCED"}


def rows(db: Session, facilities: list[Facility]) -> list[dict]:
    facility_ids = [facility.id for facility in facilities]
    if not facility_ids:
        return []

    feeds = {
        feed.facility_id: feed
        for feed in db.scalars(
            select(IntegrationFeed).where(IntegrationFeed.facility_id.in_(facility_ids))
        ).all()
    }
    marts = {
        mart.facility_id: mart
        for mart in db.scalars(
            select(MartFacilityKpi).where(MartFacilityKpi.facility_id.in_(facility_ids))
        ).all()
    }
    now = datetime.now(timezone.utc)
    result = []

    for facility in facilities:
        feed = feeds.get(facility.id)
        mart = marts.get(facility.id)
        if feed is not None:
            last_sync = as_utc(feed.last_sync_at or feed.last_success_at)
            age_hours = (
                (now - last_sync).total_seconds() / 3600 if last_sync is not None else None
            )
            status = feed.status or "NEVER_SYNCED"
            mode = feed.mode
            ingested = int(feed.rows_ingested or 0)
            quarantined = int(feed.rows_quarantined or 0)
        else:
            last_sync = as_utc(getattr(mart, "data_as_of", None))
            age_hours = getattr(mart, "feed_age_hours", None)
            status = getattr(mart, "feed_status", None) or "NEVER_SYNCED"
            mode = facility.integration_mode
            ingested = 0
            quarantined = 0

        result.append(
            {
                "facility": facility,
                "feed": feed,
                "mode": mode,
                "status": str(status).upper(),
                "last_sync_at": last_sync,
                "age_hours": age_hours,
                "rows_ingested": ingested,
                "rows_quarantined": quarantined,
            }
        )

    return result


def snapshot(db: Session, facilities: list[Facility]) -> dict:
    feed_rows = rows(db, facilities)
    healthy = sum(1 for row in feed_rows if row["status"] in HEALTHY)
    return {
        "feeds_healthy": healthy,
        "feeds_total": len(feed_rows),
        "stale_feeds": [
            {
                "id": row["facility"].id,
                "name": row["facility"].name_en,
                "status": row["status"],
                "age_hours": row["age_hours"],
            }
            for row in feed_rows
            if row["status"] not in HEALTHY
        ],
    }
