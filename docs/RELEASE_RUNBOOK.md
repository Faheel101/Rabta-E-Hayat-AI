# Rabta-e-Hayat release and demonstration runbook

This runbook operates the synthetic hackathon release. It does not authorize
production use with real donor or patient data; native-speaker, clinical,
privacy, infrastructure, and regulatory review remain mandatory.

## One-command local Docker start

Prerequisites: Docker Desktop with Compose v2 and at least 8 GB available RAM.

```bash
docker compose up --build -d
```

The first boot creates the persistent `rabta-e-hayat-data` volume and builds the
deterministic synthetic history. It can take several minutes. Later starts apply
additive migrations and reuse the volume.

```bash
docker compose ps
docker compose logs -f app
```

The service is ready only when both probes return HTTP 200:

```bash
curl --fail http://127.0.0.1:8765/health/live
curl --fail http://127.0.0.1:8765/health/ready
```

Open <http://127.0.0.1:8765>. The sign-in page lists the synthetic demonstration
accounts. The shared demonstration password is `Rabta@2026`. After signing in
as the RBC Coordinator, open <http://127.0.0.1:8765/showcase> for the timed,
tenant-scoped demonstration control.

## First use and role orientation

Every account opens a role-specific welcome on the dashboard until the user
completes or dismisses it. Open `/app/getting-started` from **Help & Workflows**
in the rail, top bar, or account menu. The page provides three real task entry
points, the role's responsibility and hand-off, shared screen semantics, safety
controls, and a short glossary. Completion is remembered per account; use
**Show welcome on dashboard again** at the bottom of the guide to restart it.

Facility-pinned users do not receive a facility switcher. Group-level users may
switch only among facilities owned by their organization. Treat a mismatch
between the visible rail and the person's assigned job as an account-role setup
issue, not as a training problem.

## Release gates

Sign in as System Administrator and open `/admin/release` first. The read-only
workspace recomputes seven live product gates, shows the complete role hand-off
chain, and proves the seeded standalone, hospital-group, RBC-network and
provincial operating models. It never runs tests or changes clinical state.

Run the in-container configuration, schema, integrity, asset, and demo-data gate:

```bash
docker compose exec app python -m scripts.release_check
```

Run the read-only role journeys and three-second local page budget:

```bash
docker compose exec app python -m scripts.demo_smoke \
  --base-url http://127.0.0.1:8000 --budget-ms 3000
```

The 169 probes cover eight accountable identities across all seven roles, both
health endpoints, role-specific guidance, the complete blood-bank surface,
network intelligence, provincial oversight, the guided showcase, emergency
control, onboarding, release acceptance and system administration. They include
expected 403 checks proving that each identity cannot type its way into an
out-of-role workspace. The probes read pages but do not approve, dispatch,
issue, transfuse, discard, declare, or otherwise alter clinical inventory.

Before packaging a release from the host environment:

```bash
python -m scripts.migrate --check
pytest -q
npm run css
docker compose config --quiet
```

Create the final JSON evidence and human-readable review dossier with one
command. The full suite runs against a disposable database copy; live journeys
are read-only. Require Docker so a missing build/runtime cannot be recorded as
a pass:

```bash
python -m scripts.release_candidate \
  --base-url http://127.0.0.1:8765 \
  --full-suite --backup-rehearsal --require-docker
```

Artifacts are written under `artifacts/release-candidate/`. A `BLOCKED` Docker
gate is an incomplete release, not a warning to ignore.

## Stop, restart, and inspect

```bash
docker compose restart app
docker compose stop app
docker compose start app
docker compose logs --tail=200 app
```

Container restarts do not rebuild data. The named volume owns the SQLite
database, and readiness refuses traffic when schema migration is required.

## Consistent backup

The SQLite backup API captures committed WAL contents. Never copy `rabta.db`
directly while the application is running.

```bash
docker compose exec app python -m scripts.backup_restore backup --output /backups
```

The command creates a `.db` file and adjacent `.db.json` manifest containing its
SHA-256 checksum. Both live in the `rabta-e-hayat-backups` volume. Copy both to
separate storage for an actual disaster-recovery copy.

## Checksum-verified restore

Restore is intentionally explicit and must run while the application is stopped.
Replace the example filename with the backup printed by the backup command.

```bash
docker compose stop app
docker compose run --rm --no-deps --entrypoint python app \
  -m scripts.backup_restore restore /backups/rabta-YYYYMMDDTHHMMSSZ.db \
  --database /data/rabta.db --yes-i-have-stopped-rabta
docker compose start app
curl --fail http://127.0.0.1:8765/health/ready
```

Before replacement, restore creates another consistent database under
`/data/pre-restore/`. A failed rehearsal therefore has a recovery point.

## Production configuration contract

`APP_ENV=production` refuses startup unless:

- `SECRET_KEY` is an unpredictable value of at least 32 characters;
- `SESSION_COOKIE_SECURE=true`;
- `SESSION_COOKIE_SAMESITE` is `strict` or `lax`;
- `TRUSTED_HOSTS` explicitly lists hostnames and does not contain `*`;
- `RABTA_SHOW_DEMO_LOGINS` is disabled.

Use TLS at the reverse proxy, forward only from known proxy addresses, keep
`AUTO_CREATE_SCHEMA=false`, run migrations as a release step, and set
`CSRF_TRUSTED_ORIGINS` only for deliberate same-organization browser origins.
The default Compose file is explicitly `APP_ENV=demo`, not production.

## Demonstration sequence

Sign in as the RBC Coordinator and begin at `/showcase`. The page reports live
tenant-scoped counts, system readiness, the fixed synthetic scenario time, and
the four forecast-quality gates. Follow its four-minute chapter timer:

1. **1:05 — vein-to-vein operations:** open the operational dashboard, then
   follow donor registration, collection, lab, processing, stock, a clinical
   request, issue, and outcome as one stateful workflow.
2. **0:50 — inventory to predictive action:** open the Command Centre and show
   stock, P10/P50/P90 demand, shortage risk, freshness, and the separately
   disclosed 22.3% decision-grain and 35.0% blood-group WAPE values.
3. **0:45 — governed movement:** open the transfer workspace and show approval,
   FEFO manifest, dispatch custody, tracking, receipt, and reconciliation; no
   recommendation moves stock automatically.
4. **0:50 — emergency readiness:** open the deterministic 1,000-run digital twin,
   compare interventions, and show that live declaration remains an explicit
   permissioned action.
5. **0:30 — integration evidence:** open Data & Integrations and show the shared
   validation, quarantine, provenance, reconciliation, and tenant-scoped API
   contract for CSV, FHIR, and HL7.

Switch the showcase to Urdu and a 390-pixel width to demonstrate native RTL
operation without a separate application. If time allows, end on DIN
traceability from donor and screening through component, shelf, custody, issue,
and outcome.

Never improvise real clinical data during the demonstration. All records and
identifiers shipped by this release are realistic synthetic data only.
