# Sprint 13 — Release-Candidate UAT and Demonstration Hardening

**Owner:** Faheel + Codex  
**Status:** Implementation complete; Docker host gate blocked — 29 August 2026  
**Release:** 0.13.0

## Outcome

Rabta-e-Hayat now has one release-acceptance contract shared by the application,
operator command, live route probe, documentation and tests. It connects every
role and stateful hand-off without granting new clinical authority or mutating
inventory merely to prove readiness.

## Implemented contract

1. **Acceptance workspace:** `/admin/release` is restricted to System
   Administrator and recomputes seven read-only product gates: runtime/schema,
   synthetic-data isolation, accounts/roles, reference data, two-person
   laboratory capacity, intelligence freshness and operating-model coverage.
2. **Role hand-offs:** eight accountable identities cover seven roles. The two
   laboratory identities remain separate because the tester cannot verify their
   own release.
3. **Workflow chain:** PH-01–05, BBO-01–07, LAB-01–04, RBC-01–04, EC-01–03,
   PA-01–03 and SA-01–05 are ordered by the work they create for the next role,
   with the expected numerical state change stated on the page and in the manual
   runbook.
4. **Operating models:** live reference data proves private standalone, sharing
   standalone, hospital-group, RBC hub/spoke and provincial-programme behavior.
5. **Operator evidence:** `python -m scripts.release_candidate` checks release
   configuration/schema/assets/integrity, additive migration drift, compiled
   CSS, isolated tests, optional live journeys, backup/restore and Docker. It
   writes JSON and Markdown evidence under `artifacts/release-candidate/`.
6. **Fail-closed environment:** test subprocesses force test mode, disable the
   intelligence worker and hide demo credentials even when the parent process
   is the running demonstration environment.
7. **Responsive dossier:** desktop and mobile use different role-matrix
   presentations; long workflow identifiers and shared-shell freshness badges
   wrap without widening the document.

## Evidence

- Release configuration, additive schema check, static assets and SQLite
  integrity: pass.
- Complete automated suite: **573 passed, 3 intentional skips**.
- Live service/role/permission evidence: **153/153 passed** across eight
  identities and seven roles; slowest page **532.3 ms** against 3,000 ms.
- Backup manifest checksum verification and atomic restore of the 999 MiB
  disposable database: pass.
- Browser QA: English and Urdu/RTL, default desktop and 390×844, release
  workspace and guided showcase; no horizontal document overflow, raw
  translation tokens or browser warnings/errors.
- Generated evidence:
  `artifacts/release-candidate/rabta-0.13.0-acceptance.json` and
  `artifacts/release-candidate/rabta-0.13.0-acceptance.md`.

## Remaining external host gate

Docker is not installed or available on this Mac. The acceptance command was run
with `--require-docker` and therefore reports the release **BLOCKED** instead of
silently passing. Once Docker Desktop is installed, run:

```bash
docker compose up --build -d
python -m scripts.release_candidate \
  --base-url http://127.0.0.1:8765 \
  --full-suite --backup-rehearsal --require-docker
docker compose restart app
curl --fail http://127.0.0.1:8765/health/ready
```

Confirm the release remains ready after restart and that the named data volume
retains the same database. No application implementation work is currently
known to be outstanding; this is a host-runtime verification.
