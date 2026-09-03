"""Consistent SQLite backup and checksum-verified restore.

Usage:

    python -m scripts.backup_restore backup --output ./backups
    python -m scripts.backup_restore restore ./backups/rabta-....db \
        --yes-i-have-stopped-rabta

The SQLite backup API includes committed WAL contents.  A raw file copy does
not, which is why this command exists instead of a runbook instruction to copy
``rabta.db``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

from config.settings import DATABASE_URL


def sqlite_path(database_url: str = DATABASE_URL) -> Path:
    url = make_url(database_url)

    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("Backup/restore currently supports file-backed SQLite only")

    return Path(url.database).expanduser().resolve()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def assert_integrity(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)

    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()

    if not result or result[0] != "ok":
        raise RuntimeError("SQLite integrity check failed")


def backup_sqlite(source: Path, output_dir: Path, *, label: str = "rabta") -> Path:
    source = source.resolve()
    output_dir = output_dir.resolve()

    if not source.is_file():
        raise FileNotFoundError(f"Database does not exist: {source}")

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = output_dir / f"{label}-{timestamp}.db"

    with tempfile.NamedTemporaryFile(
        prefix=".rabta-backup-", suffix=".tmp", dir=output_dir, delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)

    try:
        source_db = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        target_db = sqlite3.connect(temporary_path)

        try:
            source_db.backup(target_db)
        finally:
            target_db.close()
            source_db.close()

        assert_integrity(temporary_path)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)

    manifest = {
        "format": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": source.name,
        "backup": destination.name,
        "bytes": destination.stat().st_size,
        "sha256": sha256(destination),
    }
    manifest_path = destination.with_suffix(destination.suffix + ".json")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    return destination


def verify_backup(backup: Path) -> dict:
    backup = backup.resolve()

    if not backup.is_file():
        raise FileNotFoundError(f"Backup does not exist: {backup}")

    manifest_path = backup.with_suffix(backup.suffix + ".json")

    if not manifest_path.is_file():
        raise RuntimeError("Backup manifest is missing")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if manifest.get("backup") != backup.name or manifest.get("sha256") != sha256(backup):
        raise RuntimeError("Backup checksum does not match its manifest")

    assert_integrity(backup)
    return manifest


def restore_sqlite(backup: Path, destination: Path) -> Path | None:
    """Restore atomically, preserving a recoverable copy of current state."""

    backup = backup.resolve()
    destination = destination.resolve()
    verify_backup(backup)
    destination.parent.mkdir(parents=True, exist_ok=True)

    recovery = None

    if destination.is_file():
        recovery = backup_sqlite(
            destination,
            destination.parent / "pre-restore",
            label="rabta-pre-restore",
        )

    with tempfile.NamedTemporaryFile(
        prefix=".rabta-restore-", suffix=".tmp", dir=destination.parent, delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)

    try:
        source_db = sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)
        target_db = sqlite3.connect(temporary_path)

        try:
            source_db.backup(target_db)
        finally:
            target_db.close()
            source_db.close()

        assert_integrity(temporary_path)
        os.replace(temporary_path, destination)

        for suffix in ("-wal", "-shm"):
            destination.with_name(destination.name + suffix).unlink(missing_ok=True)
    finally:
        temporary_path.unlink(missing_ok=True)

    return recovery


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up or restore the Rabta database.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    backup_parser = subcommands.add_parser("backup")
    backup_parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.getenv("BACKUP_DIR") or "backups"),
    )
    backup_parser.add_argument("--database", type=Path)

    restore_parser = subcommands.add_parser("restore")
    restore_parser.add_argument("backup", type=Path)
    restore_parser.add_argument("--database", type=Path)
    restore_parser.add_argument("--yes-i-have-stopped-rabta", action="store_true")

    args = parser.parse_args()
    database = (args.database or sqlite_path()).resolve()

    if args.command == "backup":
        created = backup_sqlite(database, args.output)
        print(f"Backup created: {created}")
        print(f"Manifest: {created.with_suffix(created.suffix + '.json')}")
        return 0

    if not args.yes_i_have_stopped_rabta:
        parser.error("restore requires --yes-i-have-stopped-rabta")

    recovery = restore_sqlite(args.backup, database)
    print(f"Database restored: {database}")

    if recovery:
        print(f"Previous database preserved: {recovery}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
