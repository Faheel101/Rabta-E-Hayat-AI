# Rabta-e-Hayat — Complete MVP Delivery Plan

**Delivery window:** 16–29 August 2026  
**Team:** Faheel + Codex  
**Deployment:** Docker on a local machine  
**Data:** Realistic synthetic donor, patient, operational, and network data only

## Delivery status

- **Sprint 0 — complete (16 Aug):** product boundary, state-machine rules,
  permissions, delivery sequencing, and acceptance gates are frozen here.
- **Sprint 1 — complete (16 Aug):** the routine, urgent, emergency, partial,
  incompatible, expired, returned, not-returned, reaction, replacement, and
  cancellation paths are implemented through service and bilingual web layers.
  The focused request suite passes 24 tests, the broader operational safety
  suite passes 238 tests, and the complete web suite passes 115 tests.
- **Sprint 2 — complete (16 Aug):** facility and organization command-centre
  scopes, deterministic morning briefing, priority queues, inventory heatmap,
  P10/P50/P90 forecast exploration, projected stock, model diagnostics, expiry
  rescue, prevention signals, freshness, and equivalent Urdu/RTL surfaces are
  integrated with the persisted marts. Foreign-facility URL manipulation fails
  closed, bench roles cannot open planning intelligence, and every movement
  recommendation remains subject to human approval. The complete repository
  suite passes **460 tests**; desktop and 390px browser checks pass without
  overflow, missing translations, or console errors.
- **Forecast-quality disclosure:** the calibrated eight-origin backtest passes
  all four decision gates: facility-by-component 7-day WAPE is **22.3%**
  against a 25% ceiling, **80.6%** of series are non-inferior to seasonal naive
  against an 80% floor, P10-P90 coverage is **81.2%** against an 80% target,
  and 3-day shortage recall is **98.9%** against a 75% floor. The more volatile
  blood-group grain remains separately disclosed at 35.0% WAPE against an
  estimated 32.7% noise floor; aggregation never hides that uncertainty.
- **Sprint 3 — complete (16 Aug):** optimizer recommendations now enter a
  tenant-scoped execution workspace with accountable approval/rejection,
  structured modification, physical FEFO manifests, reservation, dispatch
  custody, in-transit state, unit-level receipt reconciliation, discrepancy,
  quarantine, missing-in-transit, cancellation, tracking, and printable A5
  barcode/QR slips. Re-solves preserve approved and completed custody history,
  cross-organization candidates require live sharing consent, and every service
  transition rechecks permission and clinical/logistics invariants. The real
  province dataset solves within its 30-second interactive guardrail (12 routes,
  741 units, 715 forecast shortages averted, 322 expiry-risk units rescued;
  feasible with a disclosed 5.97% optimality gap). The complete repository suite
  passes **475 tests**; English desktop and Urdu 390px current/history/detail
  browser checks pass without overflow, missing translations, or console errors.
- **Sprint 4 — complete (16 Aug):** the Emergency Digital Twin now provides
  deterministic, seed-reproducible 1,000-run incident modelling, configured
  presets, infrastructure degradation, road constraints, casualty placement,
  reserve interventions, consent-aware network routing, same-seed comparisons,
  and independent English/Urdu response briefs. A live declaration remains an
  explicit, typed, permissioned action; it creates an incident and physical FEFO
  transfer recommendations without moving inventory before human approval. The
  governed alert workspace adds tenant-scoped evidence, deduplication, cooldown,
  ownership, acknowledgement, escalation, resolution, notification outbox, and
  audit history. The province reference scenario completes 1,000 iterations in
  **2.245 seconds** (973 P95 planning demand, 692 Monte Carlo P95 demand, coverage
  improving from 75.3% to 86.9%, and 20 recommended transfers). The complete
  repository suite passes **481 tests**; English/Urdu desktop and 390px browser
  checks pass without overflow, missing translations, or console errors.
- **Sprint 5 — complete (16 Aug):** the governed Data & Integrations workspace
  now accepts manual CSV, simulated FHIR R4 and HL7 v2 payloads through one
  canonical adapter contract. Sources are archived before parsing; every row is
  normalized, validated, deduplicated, and either committed or quarantined with
  stable evidence and provenance. Versioned REST/OpenAPI access adds hashed,
  scoped, revocable API keys, rate limiting, and tenant/facility boundaries.
  Feed health, freshness, reconciliation, import history, remapping, error
  exports, and one-time key delivery are available in English and Urdu. Thirty
  synthetic facility feeds and ten tenant histories exercise valid, partial,
  unfulfilled, duplicate-safe, and impossible-demand paths. The complete
  repository suite passes **490 tests**; English/Urdu desktop and 390px browser
  checks pass without page overflow or missing Data & Integrations translations.
- **Sprint 6 — complete (16 Aug):** every enabled operational surface now has
  equivalent English and Urdu copy, stable clinical/eligibility terminology,
  translated transactional feedback, native RTL behavior, and responsive
  navigation, forms, tables, timelines, and actions. The shared shell adds a
  keyboard skip link, visible focus states, live regions, Escape handling,
  minimum touch targets, LTR isolation for clinical identifiers, reduced-motion
  support, and system-font fallbacks. Google Fonts and all other remote runtime
  dependencies have been removed. Strict catalog parity, local-only assets,
  semantic colors, keyboard landmarks, and representative Urdu operational
  routes are enforced by tests. The complete repository suite passes **501
  tests** with one intentional data-dependent skip; English and Urdu checks at
  the default viewport and 390×844 pass without document overflow, missing
  translation tokens, broken images, numeric display artifacts, or console
  errors.
- **Sprint 7 — implementation complete; container verification pending (17
  Aug):** production startup now fails closed on unsafe secrets, cookies, hosts,
  and demo-login disclosure. Origin-bound browser writes, strict SameSite
  sessions, security headers, structured request logs, liveness/readiness,
  non-mutating schema checks, SQLite backup/restore with integrity manifests,
  a non-root multi-stage image, persistent Compose volumes, deterministic
  first-boot seeding, and an operator release gate are integrated. The complete
  repository suite passes **510 tests** with one intentional data-dependent
  skip. All **20** live role-journey probes pass (slowest page **181.9 ms**
  against a 3,000 ms budget); English, Urdu/RTL, account controls, and 390x844
  mobile navigation pass without overflow or console errors. The host does not
  currently have a Docker CLI, so an actual clean-machine image build and
  stop/restart persistence run remain the final exit-gate verification.
- **Sprint 8 — complete (21 Aug):** forecast credibility, scenario-time
  semantics, and a premium guided demonstration are integrated. TSB demand
  smoothing was calibrated with a read-only grid harness, the production image
  now declares every forecasting runtime dependency, and the persisted
  eight-fold run passes all four decision gates while retaining the granular
  noise-floor disclosure. Every authenticated page identifies the fixed
  synthetic scenario and timestamp. A live, tenant-scoped four-minute showcase
  connects operations, forecast evidence, governed stock movement, emergency
  response, and integrations without static KPI fixtures. All **21** live role
  probes pass. The complete repository suite passes **520 tests** with one
  intentional data-dependent skip; English and Urdu desktop/390x844 checks pass
  without overflow, untranslated tokens, or browser console errors.
- **Sprint 9 — complete (22 Aug):** a zero-training, role-aware learning layer
  is integrated into the real application. Every role receives three usable
  next actions, plain-language responsibility and hand-off guidance, live queue
  context, common screen semantics, safety promises, and a clinical/forecast
  glossary. Orientation completion is versioned, persisted per account, and
  audited; Help & Workflows remains available from the rail, top bar, and
  account menu. Facility-pinned staff can no longer switch into sibling
  facilities, and bench navigation now reflects its actual work boundary.
  Mixed-direction scenario timestamps and narrow dashboard tables were corrected
  during browser QA. All **24** live role probes pass (slowest page **171.6
  ms** against a 3,000 ms budget). The complete repository suite passes **531
  tests** with one intentional data-dependent skip; coordinator and
  phlebotomist desktop plus English/Urdu 390x844 checks pass without overflow,
  untranslated tokens, or browser console errors.
- **Sprint 10 — complete (22 Aug):** end-to-end UAT now covers all seven roles,
  their guided actions, visible navigation, negative direct-URL boundaries,
  bilingual recovery, and the integrated operational chain. Operational read
  access is enforced by the same permission matrix as writes: a hidden module
  can no longer be opened by typing its URL. Unit-level inventory is isolated
  from phlebotomy and emergency roles, their dashboards show the responsible
  hand-off instead of unusable stock controls, and recovery pages retain only
  built, authorized navigation. Inventory drill-downs are semantic keyboard
  links and queue-count queries no longer read work a role cannot see. All
  **66** live probes pass across all roles, including expected refusals (slowest
  page **106.2 ms** against a 3,000 ms budget). The complete repository suite
  passes **537 tests** with one intentional data-dependent skip; phlebotomy,
  laboratory, and emergency dashboards plus English/Urdu 390x844 permission
  recovery pass without overflow, untranslated tokens, or browser console
  errors.
- **Sprint 11 — complete (28 Aug):** core operational consistency now connects
  local clinical requests to canonical demand and automatically versions and
  refreshes shortage risk, expiry rescue, coverage and dashboard marts after
  relevant transactions. A durable worker, visible freshness, safe audited
  retry, legacy-request reconciliation and live-day KPI accounting are included.
  The ordinary full-data refresh completes in **18.9 seconds** and a deliberate
  714,685-row full recovery rebuild in **49.2 seconds**. The complete suite
  passes **550 tests** with three intentional skips; English and Urdu/RTL
  desktop and 390×844 checks pass without overflow or console warnings.
- **Sprint 12 — complete (29 Aug):** a system administrator can now create a
  new tenant or attach a facility to an existing organization through a guided,
  bilingual setup; configure mode, geography, clinical capability, storage,
  feed, sharing consent, reserve policy and first accountable access; verify a
  persisted seven-part readiness contract; and explicitly activate the site.
  Drafts remain outside every operational and analytical scope. Central user
  administration adds scoped account creation, role/facility assignment,
  audited password reset, session revocation and mandatory first-login password
  replacement. The additive migration and release gate pass; the complete suite
  passes **565 tests** with three intentional skips; English and Urdu/RTL desktop
  and 390×844 browser checks pass without overflow, raw translation tokens or
  console warnings.
- **Sprint 13 — implementation complete; Docker host gate blocked (29 Aug):**
  release acceptance is now one canonical contract shared by the product,
  live role probes and operator tooling. A system administrator receives a
  bilingual release workspace covering seven live gates, eight accountable
  identities across all seven roles, the ordered operational hand-off chain,
  and five reference operating models. The evidence command runs isolated
  disposable tests, schema/assets/integrity, stylesheet compilation, live route
  boundaries, and a checksum backup/restore rehearsal, then writes JSON and
  Markdown dossiers. The complete suite passes **573 tests** with three
  intentional skips; all **153** live probes pass (slowest **532.3 ms** against
  3,000 ms); English and Urdu/RTL desktop and 390×844 browser checks pass with
  no horizontal overflow, raw translation tokens or console warnings. The
  command correctly reports the candidate **BLOCKED** because Docker is not
  installed on this host; clean image build, container restart and named-volume
  persistence remain the only incomplete exit gate.

## Product contract

Rabta-e-Hayat is one integrated blood-management platform with three bounded
capabilities:

1. **Blood-bank operations** — donor registration through transfusion outcome.
2. **Network coordination** — facility visibility, expiry rescue, and governed
   stock movement.
3. **Decision intelligence** — demand forecasting, shortage risk, optimization,
   emergency simulation, and explainable actions.

The system remains a clinical decision-support product. It may calculate,
prioritize, and recommend, but stock release and movement require an authorized
human action with an audit trail.

## Definition of complete MVP

A capability is complete only when:

- its happy path and important exception paths work through the web interface;
- all writes are tenant-scoped, permission-checked, validated, and audited;
- state transitions are explicit and illegal transitions fail closed;
- inventory, analytics, alerts, and traceability reflect the same transaction;
- English and Urdu interfaces carry equivalent meaning;
- realistic synthetic data exercises the workflow;
- service tests and web integration tests cover its safety invariants;
- empty, loading, validation, permission, and failure states are designed;
- it runs in the documented Docker environment without manual database surgery.

## Sprint 0 — Product and engineering contract

**Outcome:** One executable definition of the product and a safe delivery
baseline.

- Reconcile the operational repository with the advanced-feature specification.
- Freeze workflow state machines, permissions, audit events, and shared terms.
- Establish migration, fixture, test, and Docker conventions.
- Inventory every screen and classify it as operational, organization, network,
  or insight scope.
- Define the visual and bilingual quality bar for all new surfaces.

**Exit gate:** The scope, sequencing, clinical assumptions, and acceptance gates
in this document are represented in tests or tracked work items.

## Sprint 1 — Vein-to-vein operational chain

**Outcome:** A request can be fulfilled safely from creation to recorded patient
outcome, with every unit traceable in both directions.

- Create, prioritize, edit, cancel, and close blood requests.
- Select compatible stock using component-specific policy and FEFO ordering.
- Record compatible/incompatible crossmatches and expiry of compatibility.
- Reserve, release, substitute, and re-crossmatch units safely.
- Issue through a two-identifier handover and record custody.
- Record transfusion, return, non-return, discard, and reaction outcomes.
- Update inventory and request status transactionally.
- Extend unit and donor traceability through issue and transfusion.
- Add request queues, detail workspace, action timeline, and workload counts.

**Exit gate:** The complete request-to-outcome workflow passes service and route
tests for routine, urgent, emergency, partial, incompatible, expired, returned,
and reaction cases.

## Sprint 2 — Predictive command centre

**Outcome:** Existing forecasting, shortage-risk, and expiry-rescue engines are
usable decision workflows rather than background tables.

- Deliver a network command centre with a concise operational morning view.
- Deliver forecast exploration with P10/P50/P90, history, method, confidence,
  backtest quality, and deterministic explanation.
- Deliver shortage-risk drill-down and an actionable expiry-rescue queue.
- Add freshness, run status, fallback, data-quality, and no-data indicators.
- Improve model quality against the agreed WAPE, naive-beat, coverage, and recall
  thresholds without hiding weak series.

**Exit gate:** A user can move from a network risk to the responsible facility,
series, units, recommended action, and supporting evidence.

## Sprint 3 — Governed network transfers

**Outcome:** A recommendation can become a received, reconciled movement with a
complete chain of custody.

- Review, modify, approve, reject, and supersede optimizer recommendations.
- Pick actual FEFO units and verify compatibility and reserve constraints again.
- Dispatch, record courier/custody, mark in transit, receive, accept/reject, and
  place units into destination storage.
- Record temperature exceptions, quantity discrepancies, cancellation, and
  expiry during transit.
- Reconcile recommendation versus actual movement and feed outcomes to impact
  reporting.

**Exit gate:** No transfer changes usable inventory twice, crosses a tenant
boundary without the network-sharing contract, or bypasses approval and receipt.

## Sprint 4 — Emergency digital twin and alerts

**Status:** Complete — 16 August 2026

**Outcome:** Coordinators can model an incident, compare interventions, and
execute a governed response.

- Build the emergency scenario workspace and deterministic 1,000-run simulation.
- Support severity, casualty mix, duration, facility effects, road effects,
  donor mobilization, and stock interventions.
- Reuse forecast, compatibility, supply, expiry, and optimizer contracts.
- Show P50/P95 demand, time to shortage, lives supported, and intervention deltas.
- Add alert lifecycle, ownership, acknowledgement, escalation, and resolution.
- Surface operational evidence from the scheduled pipelines as governed alerts,
  with visible freshness, escalation, and resolution state.

**Exit gate:** Identical inputs and seed reproduce the same result, and every
recommended real-world action remains a separately authorized transaction.

## Sprint 5 — Integration and data operations

**Status:** Complete — 16 August 2026

**Outcome:** Data can enter and leave through stable, validated contracts without
coupling the platform to an undecided hospital vendor.

- Add idempotent CSV import with preview, validation, quarantine, and error export.
- Add authenticated, versioned REST endpoints and OpenAPI documentation.
- Add simulated FHIR/HL7 adapters behind the same canonical domain contract.
- Add source provenance, sync status, retry safety, and reconciliation reporting.
- Make synthetic generation produce coherent request-to-transfusion and transfer
  histories, including operational edge cases.

**Exit gate:** Re-importing the same source data is safe, invalid rows never
pollute clinical tables, and every imported record retains provenance.

## Sprint 6 — Premium bilingual experience

**Status:** Complete — 16 August 2026

**Outcome:** Every role receives a coherent, polished, accessible workspace in
English and Urdu.

- Complete equivalent Urdu coverage and right-to-left layout behavior.
- Establish approved clinical terminology with a visible review status.
- Refine responsive navigation, forms, tables, charts, timelines, and actions.
- Add consistent urgency, status, risk, confidence, and data-freshness semantics.
- Verify keyboard use, focus, contrast, error association, and reduced motion.
- Remove remote runtime assets so the local deployment remains dependable.

**Exit gate:** Every enabled screen and transactional message passes the English
and Urdu workflow checklist at desktop and narrow widths.

## Sprint 7 — Release hardening and demonstration

**Status:** Implementation complete; Docker runtime verification pending — 17
August 2026

**Outcome:** A repeatable local release demonstrates the whole platform without
manual repair or hidden setup.

- Complete migrations, secrets validation, secure-cookie modes, CSRF protection,
  logging, health checks, backup/restore, and failure recovery.
- Build the Docker services and one-command seed/start workflow.
- Run the complete automated suite plus end-to-end role journeys.
- Add performance budgets for the command centre, queues, and large tables.
- Prepare operator documentation, demo accounts, guided scenario, and recovery
  procedure.

**Exit gate:** A clean machine can start, seed, use, stop, and restart the system;
the scripted demonstration exercises all three product capabilities with no
database edits or developer-only intervention.

**Current verification:** configuration, schema, asset, database-integrity,
backup/restore, security, complete-suite, live-role, latency, bilingual, and
responsive gates pass. `docker compose build/up/down/up` cannot be exercised on
this host until Docker Desktop or another compatible Docker runtime is
available; the release is deliberately not marked fully complete before that
test.

## Sprint 8 — Forecast trust and demonstration control

**Status:** Complete — 21 August 2026

**Outcome:** The hackathon release opens with a credible, repeatable story whose
numbers come from the integrated application and whose forecast claims are
measured at the decision grain.

- Calibrate intermittent-demand forecasting through eight rolling backtest
  origins and persist the exact evidence used by the interface.
- Separate actionable facility-by-component quality from blood-group noise, and
  show both so aggregation cannot conceal uncertainty.
- Make synthetic-data mode and the fixed scenario clock explicit in health
  probes, the shared shell, and the release configuration.
- Add a tenant-scoped, bilingual four-minute showcase that links directly into
  the working operational, forecasting, transfer, simulation, and integration
  surfaces.
- Validate desktop and 390x844 layouts in English and Urdu, browser logs, role
  journeys, release checks, migrations, and the complete automated suite.

**Exit gate:** All four forecast gates pass at their declared decision grain;
the granular uncertainty remains visible; every showcase number is queried from
the signed-in tenant; all 21 live journeys and 520 automated tests pass; and the
guided story has no responsive, localization, or console regressions.

## Sprint 9 — Zero-training role guidance

**Status:** Complete — 22 August 2026

**Outcome:** A first-time user can identify their responsibility, begin a valid
task, understand the next hand-off, and recover help without separate training
material or exposure to another role's workspace.

- Add a persistent Help & Workflows surface with three concrete actions for
  every role, using the same permissioned routes and live queue counts as the
  operational system.
- Add a role-aware dashboard welcome and permanent task-first starting points,
  while allowing experienced users to dismiss the expanded orientation.
- Persist versioned completion per account and audit completion/restart without
  putting clinical state in user preferences.
- Explain scope, urgency colors, legal state transitions, decision evidence,
  safety guarantees, hand-offs, and high-frequency terminology in plain English
  and Urdu.
- Make the visible navigation match facility and role scope, including a
  server-side refusal when facility-pinned staff attempt a sibling-facility
  switch.
- Correct mobile table containment, truthful bench-role labels on the demo
  login, and mixed left-to-right timestamps inside Urdu copy.

**Exit gate:** All seven roles receive exactly three valid, role-relevant entry
points; persisted orientation works across sign-in sessions and can be restarted;
English and Urdu pass desktop and 390x844 interaction checks; all 24 live probes
and 531 automated tests pass with no responsive or translation regressions.

## Sprint 10 — End-to-end UAT and role-boundary polish

**Status:** Complete — 22 August 2026

**Outcome:** Every role reaches real work through a coherent interface, and no
visible or typed route crosses its operational responsibility boundary.

- Exercise the full donation, testing, processing, inventory, request, issue,
  outcome, transfer, alert, simulation, and integration surfaces through their
  authenticated web routes.
- Verify all seven role dashboards, three-task guides, enabled navigation links,
  and expected direct-route refusals against the same permission contract.
- Enforce local inventory, donor, collection, lab, processing, deferral sign-off,
  and clinical-request read boundaries on the server, not only in navigation.
- Replace out-of-role stock dashboards with clear hand-off guidance, and keep
  queue counts from querying work the role cannot see.
- Make permission and not-found recovery retain only built, authorized links in
  English and Urdu.
- Correct the stock table's mouse-only row behavior with semantic, keyboard-
  reachable component links.

**Exit gate:** All 66 positive and negative live probes, 537 automated tests,
release readiness, bilingual recovery, desktop layouts, and 390x844 responsive
checks pass with no missing tokens, document overflow, or browser warnings.

## Sprint 11 — Core operational consistency

**Status:** Complete — 28 August 2026

**Outcome:** Clinical actions and decision intelligence now agree without manual
pipeline intervention.

- Link local requests to canonical demand without double counting.
- Invalidate and coalesce decision refreshes in the clinical transaction.
- Rebuild demand, shortage, expiry, cover, facility KPI and impact marts as one
  versioned snapshot.
- Expose current, pending, running and failed freshness with audited retry.

**Exit gate:** See `SPRINT_11_CORE_CONSISTENCY.md` for the migration, workload,
failure-recovery and complete-suite evidence.

## Sprint 12 — Network onboarding and operational administration

**Status:** Complete — 29 August 2026

**Outcome:** The complete network foundation can be configured from the product
without scripts or direct database edits, while incomplete facilities remain
clinically invisible.

- Create or reuse an organization and choose standalone, hospital-group, RBC-
  network or provincial-programme governance.
- Configure the facility profile, parent RBC, integration mode, network consent,
  clinical services, physical storage and canonical group-level reserve policy.
- Prepare an inactive first account with a strong temporary credential.
- Re-query seven persisted readiness controls before explicit activation.
- Activate the organization, facility and prepared accounts atomically, then
  invalidate and refresh decision intelligence without inventing activity.
- Centrally create, assign, deactivate and reset users with tenant boundaries,
  audit evidence, session revocation and mandatory password replacement.

**Exit gate:** See `SPRINT_12_NETWORK_ONBOARDING.md` for authorization,
transaction, migration, regression and bilingual browser evidence.

## Sprint 13 — Release-candidate UAT and demonstration hardening

**Status:** Implementation complete; Docker host verification blocked — 29
August 2026

**Outcome:** Acceptance is a repeatable product and operator workflow rather
than a collection of disconnected test commands and notes.

- Centralize all seven roles, the independent second laboratory identity, their
  positive routes, negative URL boundaries, workflow identifiers and hand-offs.
- Add a system-administrator release workspace with live product gates, state
  evidence, the ordered vein-to-vein/network/emergency chain and operating-mode
  proof.
- Generate machine-readable JSON and a human-readable Markdown dossier from one
  non-mutating command, with isolated test environment and explicit host gates.
- Exercise a consistent backup, checksum verification and atomic restore in a
  disposable directory without replacing the demonstration database.
- Correct the manual UAT contract to use durable intelligence versions and add
  guided onboarding, activation and mandatory first-access acceptance.
- Validate English and Urdu/RTL at desktop and 390×844, correcting release-card,
  sidebar-status and mobile role-matrix overflow.

**Exit gate:** See `SPRINT_13_RELEASE_CANDIDATE.md`. All application-controlled
gates pass. Final Docker build/restart/persistence evidence requires Docker
Desktop to be installed on the host.

## Sprint 14 — Role-first UX and demonstration-data recalibration

**Status:** Implementation complete; final validation in progress — 30 August
2026

**Outcome:** Complex capability remains available without overwhelming the
operator, and the entire synthetic transactional dataset is reproducibly
rebuilt at credible facility scale.

- Lead each role with three non-duplicated **My work** destinations and place
  secondary capabilities in accessible, labelled disclosure groups.
- Collapse dense dashboard inventory evidence and low-priority system status
  while keeping active work, alert counts and freshness visible.
- Preserve stable tenants, facilities, users and policy while replacing old
  transactions with a seeded facility-scale donor, demand, inventory and
  workflow profile.
- Persist generator provenance and independently validate realism,
  distribution, capacity, lifecycle, tenancy and foreign-key integrity.

**Exit gate:** See `SPRINT_14_UX_DATA_RECALIBRATION.md`. Promotion requires the
complete automated suite and bilingual desktop/mobile browser validation.

## Working cadence

- Sprints are outcome-based, not day-bound. Multiple sprints may finish in one
  day when their exit gates pass.
- Each implementation slice follows: domain invariant → service → test → route →
  bilingual interface → integration verification.
- Faheel provides a short daily decision and acceptance session.
- Any clinical rule without an authoritative Punjab value remains configurable
  and visibly marked for authorized review.
- Scope changes enter the dependency order; they do not silently weaken an exit
  gate.
