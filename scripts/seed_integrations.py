"""Register every seeded facility through the shared adapter registry."""

from db.session import SessionLocal
from services.audit import Actor
from services.integration_service import bootstrap_simulated_feeds


def main() -> int:
    db = SessionLocal()
    try:
        created = bootstrap_simulated_feeds(
            db, Actor.system("seed-simulated-integrations")
        )
        print(f"Integration feeds ready; {created} created.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
