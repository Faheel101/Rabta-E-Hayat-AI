"""Refresh operational alerts from the persisted decision marts."""

from sqlalchemy import select

from db.models import Facility
from db.session import SessionLocal, init_db
from services import alert_service
from services.audit import Actor


def main():
    init_db()
    db = SessionLocal()
    try:
        facility_ids = list(db.scalars(select(Facility.id).where(Facility.is_active.is_(True))).all())
        summary = alert_service.sync_operational_alerts(
            db, Actor.system("alert-refresh"), facility_ids
        )
        print(f"Alerts refreshed: {summary}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
