from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from config.settings import DATABASE_URL
from db.base import Base

import db.models  # noqa: F401

IS_SQLITE = DATABASE_URL.startswith("sqlite")

connect_args = {}

if IS_SQLITE:
    # Streamlit reruns open connections from several threads.
    connect_args["check_same_thread"] = False
    connect_args["timeout"] = 30

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
)


if IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _record):
        """Write-ahead logging, so a read never blocks behind a write.

        Under the default rollback journal a single open write transaction makes
        every concurrent reader fail with "database is locked" — which a
        Streamlit app, reran on every widget interaction while a pipeline job
        holds a transaction, will hit constantly.
        """

        cursor = dbapi_connection.cursor()

        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)