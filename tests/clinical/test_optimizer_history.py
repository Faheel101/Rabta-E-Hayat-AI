"""A re-solve retires recommendations without erasing custody history."""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from db.base import Base
from db.models import AuditLog, Transfer, TransferPlan
from scripts.run_optimizer import supersede_previous_plan


def test_resolve_supersedes_only_unapproved_rows_and_preserves_history():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as db:
        old = TransferPlan(id="old-plan", status="GENERATED", created_by="optimizer")
        recommended = Transfer(
            id="recommended",
            plan_id=old.id,
            from_facility_id="source",
            to_facility_id="destination",
            component_id=2,
            blood_group_id=1,
            units=1,
            unit_ids=["unit-1"],
            status="RECOMMENDED",
        )
        approved = Transfer(
            id="approved",
            plan_id=old.id,
            from_facility_id="source",
            to_facility_id="destination",
            component_id=2,
            blood_group_id=1,
            units=1,
            unit_ids=["unit-2"],
            status="APPROVED",
        )
        db.add_all([old, recommended, approved])
        db.flush()

        plans, rows = supersede_previous_plan(db, "replacement-plan")
        db.add(
            TransferPlan(
                id="replacement-plan",
                status="GENERATED",
                created_by="optimizer",
            )
        )
        db.commit()

        assert (plans, rows) == (1, 1)
        assert old.status == "SUPERSEDED"
        assert recommended.status == "SUPERSEDED"
        assert approved.status == "APPROVED"
        assert db.get(Transfer, approved.id) is not None
        assert len(db.scalars(select(TransferPlan)).all()) == 2

        audit = db.scalars(
            select(AuditLog).where(AuditLog.action == "transfer_plan.supersede")
        ).one()
        assert audit.entity_id == old.id
        assert audit.after_json["replacement_plan_id"] == "replacement-plan"
        assert audit.after_json["recommendations_superseded"] == 1
