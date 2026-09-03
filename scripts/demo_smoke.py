"""Read-only role journeys against a running synthetic demonstration."""

from __future__ import annotations

import argparse
import json
import os
import time

import httpx

from core.release import ACCEPTANCE_IDENTITIES


TRANSLATION_TOKEN_PREFIXES = (
    "[ops.",
    "[cc.",
    "[fc.",
    "[ex.",
    "[tr.",
    "[sim.",
    "[alerts.",
    "[data.",
    "[showcase.",
    "[onboarding.",
    "[governance.",
    "[release.",
    "[evidence.",
)


def run(base_url: str, password: str, budget_ms: float) -> tuple[bool, list[dict]]:
    """Probe every release identity, positive route, and negative boundary."""

    results: list[dict] = []
    healthy = True

    with httpx.Client(base_url=base_url, follow_redirects=True, timeout=15.0) as probe:
        for health_path in ("/health/live", "/health/ready"):
            started = time.perf_counter()
            response = probe.get(health_path)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            passed = response.status_code == 200 and elapsed_ms <= budget_ms
            healthy &= passed
            results.append(
                {
                    "journey": "SERVICE",
                    "identity": "Service health",
                    "role": "SERVICE",
                    "path": health_path,
                    "status": response.status_code,
                    "expected_status": 200,
                    "ms": elapsed_ms,
                    "budget_ms": budget_ms,
                    "passed": passed,
                }
            )

    for identity in ACCEPTANCE_IDENTITIES:
        with httpx.Client(base_url=base_url, follow_redirects=True, timeout=15.0) as client:
            login = client.post(
                "/login",
                data={"email": identity.email, "password": password},
            )

            if login.status_code != 200 or str(login.url).endswith("/login"):
                healthy = False
                results.append(
                    {
                        "journey": identity.code,
                        "identity": identity.name,
                        "role": identity.role,
                        "path": "/login",
                        "status": login.status_code,
                        "expected_status": 200,
                        "ms": 0.0,
                        "budget_ms": budget_ms,
                        "passed": False,
                    }
                )
                continue

            for path in identity.allowed_paths:
                started = time.perf_counter()
                response = client.get(path)
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                body = response.text
                passed = (
                    response.status_code == 200
                    and elapsed_ms <= budget_ms
                    and "Something went wrong" not in body
                    and not any(token in body for token in TRANSLATION_TOKEN_PREFIXES)
                )
                healthy &= passed
                results.append(
                    {
                        "journey": identity.code,
                        "identity": identity.name,
                        "role": identity.role,
                        "path": path,
                        "status": response.status_code,
                        "expected_status": 200,
                        "ms": elapsed_ms,
                        "budget_ms": budget_ms,
                        "passed": passed,
                    }
                )

            for path in identity.denied_paths:
                started = time.perf_counter()
                response = client.get(path)
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                passed = response.status_code == 403 and elapsed_ms <= budget_ms
                healthy &= passed
                results.append(
                    {
                        "journey": identity.code,
                        "identity": identity.name,
                        "role": identity.role,
                        "path": path,
                        "status": response.status_code,
                        "expected_status": 403,
                        "ms": elapsed_ms,
                        "budget_ms": budget_ms,
                        "passed": passed,
                    }
                )

    return healthy, results


def summary(results: list[dict]) -> dict:
    passed = sum(1 for result in results if result["passed"])
    durations = [result["ms"] for result in results]
    return {
        "probes": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "identities": len(
            {
                result["journey"]
                for result in results
                if result["role"] != "SERVICE"
            }
        ),
        "roles": len(
            {result["role"] for result in results if result["role"] != "SERVICE"}
        ),
        "slowest_ms": max(durations, default=0.0),
    }


def as_markdown(results: list[dict], base_url: str) -> str:
    totals = summary(results)
    state = "PASS" if totals["failed"] == 0 else "FAIL"
    lines = [
        "# Rabta-e-Hayat live role evidence",
        "",
        f"**Status:** {state}  ",
        f"**Target:** `{base_url}`  ",
        f"**Evidence:** {totals['passed']}/{totals['probes']} probes across "
        f"{totals['identities']} identities and {totals['roles']} roles  ",
        f"**Slowest page:** {totals['slowest_ms']:.1f} ms",
        "",
        "| Result | Identity | Route | Expected | Actual | Time |",
        "|---|---|---|---:|---:|---:|",
    ]
    for result in results:
        state = "PASS" if result["passed"] else "FAIL"
        lines.append(
            f"| {state} | {result['identity']} | `{result['path']}` | "
            f"{result['expected_status']} | {result['status']} | {result['ms']:.1f} ms |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Rabta demonstration role journeys.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--budget-ms", type=float, default=3000.0)
    parser.add_argument(
        "--password",
        default=os.getenv("RABTA_DEMO_PASSWORD") or "Rabta@2026",
    )
    parser.add_argument(
        "--format", choices=("text", "json", "markdown"), default="text"
    )
    args = parser.parse_args()
    healthy, results = run(args.base_url, args.password, args.budget_ms)

    if args.format == "json":
        print(
            json.dumps(
                {
                    "status": "pass" if healthy else "fail",
                    "summary": summary(results),
                    "results": results,
                },
                indent=2,
                sort_keys=True,
            )
        )
    elif args.format == "markdown":
        print(as_markdown(results, args.base_url), end="")
    else:
        for result in results:
            duration = f" {result['ms']:.1f}ms" if result.get("ms") else ""
            state = "PASS" if result["passed"] else "FAIL"
            print(
                f"{state:4s}  {result['journey']:<12s} "
                f"{result['path']}{duration}"
            )
        totals = summary(results)
        print(
            f"\n{totals['passed']}/{totals['probes']} probes passed across "
            f"{totals['identities']} identities ({totals['roles']} roles); "
            f"slowest {totals['slowest_ms']:.1f}ms."
        )

    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
