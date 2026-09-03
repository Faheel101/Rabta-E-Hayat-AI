"""One evidence-producing release-candidate acceptance command.

Examples:

    python -m scripts.release_candidate --base-url http://127.0.0.1:8765
    python -m scripts.release_candidate --full-suite --require-docker

The command never mutates clinical data.  Tests use disposable SQLite copies;
live journeys are GET-only after authentication.  Docker is reported as a
separate host gate so a missing runtime can never be mistaken for a pass.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from config.settings import APP_NAME, APP_VERSION, BASE_DIR, DATABASE_URL
from db.session import SessionLocal
from scripts.backup_restore import backup_sqlite, restore_sqlite, sqlite_path, verify_backup
from scripts.demo_smoke import as_markdown as live_markdown
from scripts.demo_smoke import run as run_live_journeys
from scripts.demo_smoke import summary as live_summary
from scripts.release_check import run_checks
from services.release_acceptance import acceptance_snapshot


@dataclass(frozen=True)
class GateResult:
    code: str
    label: str
    status: str
    required: bool
    detail: str
    command: str | None = None
    duration_seconds: float | None = None


def _run_command(
    code: str,
    label: str,
    command: list[str],
    *,
    required: bool = True,
    timeout: int = 900,
    environment_overrides: dict[str, str] | None = None,
) -> GateResult:
    started = datetime.now(timezone.utc)
    environment = os.environ.copy()
    environment.update(environment_overrides or {})
    try:
        completed = subprocess.run(
            command,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        duration = (datetime.now(timezone.utc) - started).total_seconds()
        return GateResult(
            code,
            label,
            "FAIL",
            required,
            f"{type(error).__name__}: {error}",
            " ".join(command),
            round(duration, 2),
        )

    duration = (datetime.now(timezone.utc) - started).total_seconds()
    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    detail = output[-4000:] if output else f"exit {completed.returncode}"
    return GateResult(
        code,
        label,
        "PASS" if completed.returncode == 0 else "FAIL",
        required,
        detail,
        " ".join(command),
        round(duration, 2),
    )


def _docker_gate(*, required: bool) -> GateResult:
    if shutil.which("docker") is None:
        return GateResult(
            "docker",
            "Docker Compose contract",
            "BLOCKED",
            required,
            "Docker CLI is not installed or is not on PATH; build, restart and volume persistence remain unverified on this host.",
            "docker compose config --quiet",
        )
    return _run_command(
        "docker",
        "Docker Compose contract",
        ["docker", "compose", "config", "--quiet"],
        required=required,
        timeout=60,
    )


def _backup_rehearsal() -> GateResult:
    started = datetime.now(timezone.utc)
    try:
        source = sqlite_path(DATABASE_URL)
        with tempfile.TemporaryDirectory(prefix="rabta-release-restore-") as directory:
            root = Path(directory)
            backup = backup_sqlite(source, root / "backups", label="acceptance")
            manifest = verify_backup(backup)
            restored = root / "restored.db"
            restore_sqlite(backup, restored)
            restored_manifest = {
                "bytes": restored.stat().st_size,
                "backup_bytes": manifest["bytes"],
            }
        status = "PASS"
        detail = f"Checksum verified and restored atomically ({restored_manifest['bytes']} bytes)."
    except Exception as error:  # pragma: no cover - environment-specific evidence
        status = "FAIL"
        detail = f"{type(error).__name__}: {error}"
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    return GateResult(
        "backup_restore",
        "Backup and restore rehearsal",
        status,
        True,
        detail,
        "python -m scripts.release_candidate --backup-rehearsal",
        round(duration, 2),
    )


def _database_evidence() -> dict:
    db = SessionLocal()
    try:
        snapshot = acceptance_snapshot(db)
    finally:
        db.close()
    return {
        "release_ready": snapshot["release_ready"],
        "scenario_date": snapshot["scenario_date"].isoformat(),
        "gates": [dict(gate) for gate in snapshot["gates"]],
        "counts": snapshot["counts"],
        "identities": [
            {**asdict(item["contract"]), "ready": item["ready"]}
            for item in snapshot["identities"]
        ],
        "workflows": [asdict(item) for item in snapshot["workflows"]],
        "modes": [
            {**asdict(item["contract"]), "ready": item["ready"], "detail": item["detail"]}
            for item in snapshot["modes"]
        ],
    }


def _overall(gates: list[GateResult]) -> str:
    required = [gate for gate in gates if gate.required]
    if any(gate.status == "FAIL" for gate in required):
        return "FAIL"
    if any(gate.status == "BLOCKED" for gate in required):
        return "BLOCKED"
    return "PASS"


def render_markdown(report: dict) -> str:
    evidence = report["database_evidence"]
    lines = [
        f"# {report['service']} release-candidate evidence",
        "",
        f"**Release:** {report['version']}  ",
        f"**Status:** {report['status']}  ",
        f"**Generated:** {report['generated_at']}  ",
        f"**Scenario:** synthetic, fixed at {evidence['scenario_date']}",
        "",
        "## Host and release gates",
        "",
        "| Gate | Result | Required | Evidence |",
        "|---|---|---:|---|",
    ]
    for gate in report["gates"]:
        detail = gate["detail"].splitlines()[-1].replace("|", "\\|")
        lines.append(
            f"| {gate['label']} | **{gate['status']}** | "
            f"{'Yes' if gate['required'] else 'No'} | {detail} |"
        )

    counts = evidence["counts"]
    lines.extend(
        [
            "",
            "## Live database evidence",
            "",
            f"- {counts['organizations']} active organizations and {counts['facilities']} active facilities",
            f"- {counts['donors']} donors, {counts['donations']} donations, and {counts['available_units']} available screened units",
            f"- {counts['open_requests']} open requests, {counts['transfers']} governed transfers, and {counts['active_alerts']} active alerts",
            f"- {counts['audit_events']} audit events and {counts['two_person_lab_facilities']} facilities with two-person laboratory release capacity",
            "",
            "## Role acceptance",
            "",
            "| Identity | Role | Workflows | Ready |",
            "|---|---|---|---:|",
        ]
    )
    for identity in evidence["identities"]:
        lines.append(
            f"| {identity['name']} | `{identity['role']}` | "
            f"{' · '.join(identity['workflow_ids'])} | {'Yes' if identity['ready'] else 'No'} |"
        )

    lines.extend(
        [
            "",
            "## Operating-model evidence",
            "",
            "| Mode | Reference | Ready |",
            "|---|---|---:|",
        ]
    )
    for mode in evidence["modes"]:
        lines.append(
            f"| {mode['code']} | {mode['detail']} | {'Yes' if mode['ready'] else 'No'} |"
        )

    live = report.get("live_journeys")
    if live:
        lines.extend(["", live_markdown(live["results"], live["base_url"]).rstrip()])

    lines.extend(
        [
            "",
            "## Decision",
            "",
            "A PASS means every required automated and host gate selected for this run passed. "
            "A BLOCKED Docker gate is not a pass and must be completed on a host with Docker Desktop before final packaging.",
            "",
        ]
    )
    return "\n".join(lines)


def build_report(args: argparse.Namespace) -> dict:
    gates: list[GateResult] = []
    release_ok, release_report = run_checks()
    gates.append(
        GateResult(
            "release",
            "Configuration, schema, assets and integrity",
            "PASS" if release_ok else "FAIL",
            True,
            json.dumps(release_report["checks"], sort_keys=True, default=str),
            "python -m scripts.release_check --json",
        )
    )
    gates.append(
        _run_command(
            "migration",
            "Additive schema drift",
            [sys.executable, "-m", "scripts.migrate", "--check"],
        )
    )
    gates.append(
        _run_command(
            "css",
            "Compiled local stylesheet",
            ["npm", "run", "css"],
            timeout=120,
        )
    )

    if not args.skip_tests:
        test_command = [sys.executable, "-m", "pytest", "-q", "--tb=short", "--maxfail=1"]
        if not args.full_suite:
            test_command.extend(
                [
                    "tests/release",
                    "tests/web/test_sprint10_uat.py",
                    "tests/web/test_network_onboarding_web.py",
                ]
            )
        gates.append(
            _run_command(
                "tests",
                "Full automated suite" if args.full_suite else "Focused release suite",
                test_command,
                timeout=1200,
                environment_overrides={
                    "APP_ENV": "test",
                    "INTELLIGENCE_REFRESH_ENABLED": "false",
                    "RABTA_SHOW_DEMO_LOGINS": "0",
                },
            )
        )

    gates.append(_docker_gate(required=args.require_docker))
    if args.backup_rehearsal:
        gates.append(_backup_rehearsal())

    live = None
    if args.base_url:
        healthy, results = run_live_journeys(
            args.base_url,
            args.password,
            args.budget_ms,
        )
        totals = live_summary(results)
        gates.append(
            GateResult(
                "live_journeys",
                "Live role and permission journeys",
                "PASS" if healthy else "FAIL",
                True,
                f"{totals['passed']}/{totals['probes']} passed; slowest {totals['slowest_ms']:.1f}ms",
                f"python -m scripts.demo_smoke --base-url {args.base_url}",
            )
        )
        live = {"base_url": args.base_url, "summary": totals, "results": results}

    report = {
        "format": 1,
        "service": APP_NAME,
        "version": APP_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": _overall(gates),
        "gates": [asdict(gate) for gate in gates],
        "database_evidence": _database_evidence(),
    }
    if live:
        report["live_journeys"] = live
    return report


def _safe_release_name(version: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", version).strip("-") or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description="Create Rabta release-candidate evidence.")
    parser.add_argument("--base-url", help="Running app URL for live read-only role probes")
    parser.add_argument("--budget-ms", type=float, default=3000.0)
    parser.add_argument(
        "--password",
        default=os.getenv("RABTA_DEMO_PASSWORD") or "Rabta@2026",
    )
    parser.add_argument("--full-suite", action="store_true")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--backup-rehearsal", action="store_true")
    parser.add_argument("--require-docker", action="store_true")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=BASE_DIR / "artifacts" / "release-candidate",
    )
    args = parser.parse_args()

    report = build_report(args)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"rabta-{_safe_release_name(APP_VERSION)}-acceptance"
    json_path = args.output_dir / f"{stem}.json"
    markdown_path = args.output_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")

    print(f"{APP_NAME} {APP_VERSION}: {report['status']}")
    for gate in report["gates"]:
        marker = "✓" if gate["status"] == "PASS" else "!"
        print(f"  {marker} {gate['label']}: {gate['status']}")
    print(f"JSON evidence: {json_path}")
    print(f"Review dossier: {markdown_path}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
