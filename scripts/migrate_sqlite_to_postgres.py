"""Copy a populated Rabta SQLite database into an empty PostgreSQL database.

The target URL is read from ``DATABASE_URL_UNPOOLED`` (preferred for a bulk
load) or ``DATABASE_URL``. Values are never printed. The command is resumable at
whole-table granularity and refuses to overwrite a partially populated table.

    python -m scripts.migrate_sqlite_to_postgres ./rabta.db --env-file .env.local
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import String, create_engine, func, inspect, select, text

from db.base import Base
import db.models  # noqa: F401  (registers every model with Base.metadata)


def _count(connection, table) -> int:
    return int(connection.scalar(select(func.count()).select_from(table)) or 0)


def _validate_source_strings(source_engine) -> None:
    """Catch values SQLite accepted that PostgreSQL VARCHAR will reject."""

    failures: list[str] = []

    with source_engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                if not isinstance(column.type, String) or not column.type.length:
                    continue

                maximum = connection.scalar(
                    select(func.max(func.length(column))).select_from(table)
                )
                if maximum and maximum > column.type.length:
                    failures.append(
                        f"{table.name}.{column.name} "
                        f"({maximum} > {column.type.length})"
                    )

    if failures:
        raise SystemExit(
            "Source values exceed modeled VARCHAR lengths: " + ", ".join(failures)
        )


def _reset_sequences(target_engine) -> None:
    """Advance PostgreSQL sequences after explicit integer IDs are imported."""

    preparer = target_engine.dialect.identifier_preparer

    with target_engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            for column in table.primary_key.columns:
                if not column.autoincrement or column.type.python_type is not int:
                    continue

                sequence = connection.scalar(
                    text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                    {"table_name": table.name, "column_name": column.name},
                )
                if not sequence:
                    continue

                table_name = preparer.quote(table.name)
                column_name = preparer.quote(column.name)
                connection.execute(
                    text(
                        "SELECT setval(CAST(:sequence AS regclass), "
                        f"COALESCE(MAX({column_name}), 1), "
                        f"MAX({column_name}) IS NOT NULL) FROM {table_name}"
                    ),
                    {"sequence": sequence},
                )


def _widen_target_varchars(target_engine) -> None:
    """Apply safe VARCHAR widenings when resuming after a model correction."""

    inspector = inspect(target_engine)
    preparer = target_engine.dialect.identifier_preparer

    with target_engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            live_columns = {
                column["name"]: column for column in inspector.get_columns(table.name)
            }
            for column in table.columns:
                if not isinstance(column.type, String) or not column.type.length:
                    continue

                live = live_columns.get(column.name)
                live_length = getattr(live["type"], "length", None) if live else None
                if not live_length or live_length >= column.type.length:
                    continue

                table_name = preparer.quote(table.name)
                column_name = preparer.quote(column.name)
                connection.execute(
                    text(
                        f"ALTER TABLE {table_name} ALTER COLUMN {column_name} "
                        f"TYPE VARCHAR({column.type.length})"
                    )
                )
                print(
                    f"  ~ {table.name}.{column.name}: "
                    f"VARCHAR({live_length}) to VARCHAR({column.type.length})"
                )


def migrate(source_path: Path, target_url: str, batch_size: int) -> None:
    source_engine = create_engine(f"sqlite:///{source_path.resolve().as_posix()}")
    target_engine = create_engine(target_url, pool_pre_ping=True)

    if target_engine.dialect.name != "postgresql":
        raise SystemExit("Target must be PostgreSQL; refusing to continue.")

    _validate_source_strings(source_engine)
    Base.metadata.create_all(target_engine)
    _widen_target_varchars(target_engine)
    existing_tables = set(inspect(target_engine).get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            raise RuntimeError(f"Target table was not created: {table.name}")

        with source_engine.connect() as source, target_engine.connect() as target:
            source_count = _count(source, table)
            target_count = _count(target, table)

        if target_count == source_count:
            print(f"  = {table.name}: {source_count:,} rows")
            continue

        if target_count:
            raise SystemExit(
                f"Target table {table.name} has {target_count:,} rows; "
                "refusing to replace partial data."
            )

        inserted = 0
        with source_engine.connect() as source, target_engine.begin() as target:
            result = source.execution_options(stream_results=True).execute(select(table))

            while rows := result.mappings().fetchmany(batch_size):
                target.execute(table.insert(), [dict(row) for row in rows])
                inserted += len(rows)

        if inserted != source_count:
            raise RuntimeError(
                f"Row-count mismatch for {table.name}: {inserted:,}/{source_count:,}"
            )

        print(f"  + {table.name}: {inserted:,} rows")

    _reset_sequences(target_engine)
    print("Migration complete; every modeled table matches the source row count.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Populated SQLite database")
    parser.add_argument("--env-file", type=Path, default=Path(".env.local"))
    parser.add_argument("--batch-size", type=int, default=2_000)
    args = parser.parse_args()

    if not args.source.is_file():
        parser.error(f"SQLite database not found: {args.source}")
    if args.batch_size < 100 or args.batch_size > 10_000:
        parser.error("--batch-size must be between 100 and 10000")

    load_dotenv(args.env_file, override=False)
    target_url = os.getenv("DATABASE_URL_UNPOOLED") or os.getenv("DATABASE_URL") or ""
    if not target_url:
        raise SystemExit("DATABASE_URL_UNPOOLED or DATABASE_URL is required.")

    migrate(args.source, target_url, args.batch_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
