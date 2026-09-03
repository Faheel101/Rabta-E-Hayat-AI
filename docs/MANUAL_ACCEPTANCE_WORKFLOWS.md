# Rabta-e-Hayat Manual Acceptance Workflows

This is the role-by-role acceptance script for the integrated synthetic MVP. It is deliberately transaction-led: every action has an expected state transition, a visible counter change, and an accountable handoff.

## 1. Test rules

1. Use a resettable copy of the synthetic database. Do not use a database that must be preserved for the final demonstration.
2. Use one run label everywhere, for example `UAT-A`. Prefix donor names, patient references, notes, incident names, container IDs, and reconciliation references with that label.
3. Before an action, record the visible number and the selected facility/scope. After the action, refresh the destination page and record the new number.
4. Test one mutation at a time. If a number does not move by the expected delta, stop at that step and report the page, role, facility, scope, before value, after value, and record code/DIN.
5. Do not infer that an aggregate page is correct from a success toast. Verify the record in its operational queue, its status timeline, its inventory effect, and its audit evidence.
6. A recommendation, forecast, simulation, or optimizer run must never move inventory. Only a human approval or clinical custody action may change a unit's state.
7. Begin at **Admin → Release acceptance** as System Administrator. All seven live gates must pass before recording manual workflow evidence.
8. After the manual run, create a machine-readable dossier with `python -m scripts.release_candidate --base-url http://127.0.0.1:8765 --full-suite`. A missing Docker gate is recorded as blocked, never as passed.

### Shared credentials

All seeded accounts use password `Rabta@2026`.

| Test identity | Role | Main workspace | Email |
|---|---|---|---|
| Nasreen Bibi | Phlebotomist | Jinnah Lahore collection bench | `n.bibi@punjab-teaching.rabta.pk` |
| Rizwan Aslam | Lab Technologist A | Jinnah Lahore lab/processing | `r.aslam@punjab-teaching.rabta.pk` |
| Farah Noor | Lab Technologist B | Jinnah Lahore independent release | `f.noor@punjab-teaching.rabta.pk` |
| Dr. Ahmed Raza | Blood Bank Officer | Jinnah Lahore clinical operations | `dr.ahmed@punjab-teaching.rabta.pk` |
| Sadia Fatima | RBC Coordinator | Punjab Teaching network | `s.fatima@punjab-teaching.rabta.pk` |
| Dr. Tariq Mahmood | Provincial Administrator | Punjab oversight | `dr.tariq@south-punjab-dhq.rabta.pk` |
| Provincial Emergency Cell | Emergency Controller | Province emergency response | `control.room@south-punjab-dhq.rabta.pk` |
| System Administrator | System Admin | Platform/integration administration | `admin@punjab-teaching.rabta.pk` |

Supporting mode-validation accounts:

| Operating model | Email | Expected boundary |
|---|---|---|
| Standalone and opted out | `a.hussain@shaukat-khanum.rabta.pk` | Local BBMIS works; cross-organization inventory sharing is disabled. |
| Standalone and opted in | `dr.zainab@children-trust.rabta.pk` | Local BBMIS works; safe inventory signals may participate in the network. |
| RBC hub coordinator | `dr.khan@rbc-punjab-north.rabta.pk` | RBC Lahore and its spoke scope. |

## 2. Number consistency contract

### Immediate transactional numbers

These must update as soon as the next page is loaded:

- donor register and donor status;
- session Screened, Accepted, Deferred, Collected, Did not donate, and target shortfall;
- Lab pending and Processing pending navigation badges;
- lab worklists, component yield, live inventory tables, unit trace and storage status;
- open clinical requests, request status, units requested/issued, replacement balance and custody status;
- transfer status, manifest count, source/destination unit states and tracking timeline;
- open alerts, occurrence count, acknowledgement ownership and resolution;
- reconciliation issues, import row counts, API clients, user status, facility settings and audit entries.

### Derived intelligence numbers

The Dashboard stock KPIs, days of cover, shortage risk, Facilities comparison, Analytics, Command Centre, Forecast and Expiry Rescue are analytical snapshots. Their source tables are rebuilt by the intelligence pipeline rather than by each clinical transaction.

For acceptance, apply this rule:

1. Verify the operational record immediately.
2. Confirm the shared freshness indicator changes to pending/updating after a relevant write and record its source version.
3. Allow the coalesced background refresh to complete. If it reports failed, an authorized administrator uses the audited **Refresh intelligence** retry in Admin; never run a full database rebuild because that would regenerate the scenario and erase the UAT transaction.
4. When the status returns to clean, confirm completed version equals source version and record the new snapshot timestamp.
5. Verify Dashboard, days of cover, shortage risk, Facilities, Analytics, Command Centre, Forecast and Expiry Rescue changed exactly once where the transaction affects that measure, using the same facility and scope.

The platform is transactionally immediate and analytically version-consistent: a success toast is not considered analytical evidence until the durable refresh state is clean at the current source version.

## 3. Master testing order

Run the tests in this order because each role creates work for the next role:

1. Phlebotomist: donor registration, screening, collection and deferral.
2. Blood Bank Officer: contested-deferral sign-off.
3. Lab Technologist A: component separation and TTI testing.
4. Lab Technologist B: independent release.
5. Blood Bank Officer: request, crossmatch, issue, transfusion and exception custody.
6. RBC Coordinator: network insight, integration and governed transfer execution.
7. Emergency Controller: preparedness, comparison, live declaration and emergency plan.
8. Provincial Administrator: province scope, alerts, analytics, optimizer policy and governed administration.
9. System Administrator: platform boundaries, users, facilities, integrations, audit and optimizer execution.
10. Mode validation: standalone opted out, standalone opted in, RBC network and province scope.

## 4. Phlebotomist workflow

### PH-01 Open and close a collection session

1. Sign in as Nasreen Bibi and confirm the active facility is Jinnah Lahore.
2. Open **Collection Sessions** and record the number of open sessions.
3. Open an outreach session:
   - venue: `UAT-A Government College`;
   - organiser: `UAT-A Red Crescent`;
   - target: `2` units.
4. Verify the session is `OPEN`, target is `2`, and all session outcome counters initially read zero.
5. Keep the session open for PH-02 to PH-04.

Expected delta: open sessions `+1`; inventory, donors, screened and collected remain unchanged.

### PH-02 Accepted donor and collection

1. On the session, register `UAT-A Accepted Donor` with a valid adult date of birth and a blood group.
2. Start screening. Verify the donor appears under unfinished screenings, but the **Screened** counter does not move while the record is a draft.
3. Enter haemoglobin `15.1`, weight `72`, blood pressure `120/78`, pulse `72`, temperature `36.7`, and negative history answers.
4. Verify the live verdict says **Accepted** and permits `450 mL`.
5. Complete screening.
6. Record a `WHOLE_BLOOD` donation using a `TRIPLE` bag.
7. Copy the generated DIN; it is the trace key for all later handoffs.

Expected deltas:

- donor register `+1`;
- unfinished screenings `+1` at draft, then `-1` at completion;
- Screened `+1`, Accepted `+1`;
- Did not donate temporarily `+1` after acceptance, then `-1` after collection;
- Collected `+1`;
- donor total donations `+1` and availability becomes `RECENTLY_DONATED`;
- Lab pending `+1` and Processing pending `+1`;
- one whole-blood unit exists in `QUARANTINE` with screening `PENDING`;
- Available inventory does not increase yet.

### PH-03 Ordinary deferral

1. Register `UAT-A Low Hb Donor` in the same session.
2. Enter haemoglobin `9.2` and weight `70`.
3. Verify the live verdict becomes **Deferred** before questionnaire completion.
4. Complete screening and verify no collection action is offered.

Expected deltas: donor register `+1`; Screened `+1`; Deferred `+1`; Collected unchanged; donor status becomes temporarily deferred; no blood-unit row is created.

### PH-04 Contested deferral for clinical handoff

1. Register `UAT-A Signoff Donor`.
2. Enter acceptable vitals.
3. In infection history select a configured contested answer such as hepatitis B history.
4. Complete screening.
5. Verify the donor is deferred and the interface says clinical sign-off is required.

Expected deltas: Screened `+1`; Deferred `+1`; Clinical Sign-off pending `+1`; Collected and inventory unchanged.

### PH-05 Close the session

1. Return to the session after all three screenings.
2. Confirm Screened `3`, Accepted `1`, Deferred `2`, Collected `1`, and target `2`.
3. Close the session.
4. Verify status `CLOSED` and a shortfall of `1` is shown and audited.

## 5. Blood Bank Officer clinical sign-off

### BBO-01 Decide the contested deferral

1. Sign in as Dr. Ahmed.
2. Confirm the Clinical Sign-off badge includes the case created in PH-04.
3. Open the case and verify both the currently applied rule and alternative rule are visible.
4. Choose **Lift** or **Uphold** and provide a meaningful clinical reason. Record which branch was used.
5. Reopen the donor record.

Expected deltas: Clinical Sign-off pending `-1`; one audit event is added. Lift changes the donor back to an eligible/available state when no other deferral is active. Uphold keeps the deferral and records reviewer, time and rationale.

## 6. Laboratory and component-processing workflow

### LAB-01 Separate the collected triple bag

1. Sign in as Rizwan Aslam.
2. Open **Component Processing** and locate the DIN from PH-02.
3. Verify the recipe shows `PRBC`, `PLT_RD`, and `FFP`, with their remaining separation windows.
4. Produce all three components.

Expected deltas:

- Processing pending `-1`;
- separation count `+1`, units expected `+3`, units produced `+3`, units lost unchanged;
- parent whole-blood unit becomes `SEPARATED` and is not double-counted;
- three component units are created in `QUARANTINE/PENDING` because the donation is not released;
- Available inventory remains unchanged.

### LAB-02 Complete the TTI panel as Technologist A

1. Open **Laboratory** and locate the same DIN.
2. For every required marker, open a run with a UAT kit lot, record valid controls, and submit a non-reactive result for the DIN.
3. Verify the donation moves from **Awaiting results** to **Ready to release** when the configured panel is complete.
4. Attempt to release it as Rizwan.

Expected result: self-release is refused visibly by the two-person rule; donation remains unreleased; Lab pending remains `+1` relative to the original baseline; component units stay quarantined.

### LAB-03 Independent release as Technologist B

1. Sign out and sign in as Farah Noor.
2. Open **Laboratory**, locate the DIN under Ready to release, and release it.
3. Open Inventory and search the DIN prefix.

Expected deltas:

- Lab pending `-1`;
- donation status becomes `RELEASED` with Rizwan recorded as tester and Farah as verifier;
- all three separated units move `QUARANTINE/PENDING → AVAILABLE/PASSED`;
- live available inventory `+3`;
- no whole-blood parent is added to available inventory;
- each component has a traceable collection, processing, test and release chain.

### LAB-04 Reactive-result exception on a different UAT donation

1. Use a second accepted UAT donation, not the DIN needed for the patient workflow.
2. Record one marker as reactive.
3. Verify the donation is quarantined, its units are discarded/failed, a confirmatory test is created, and the donor becomes `AWAITING_TTI_CONFIRMATION`.
4. Record a confirmatory result through the permitted second-person flow.

Expected deltas: failed/discarded units increase by the number created for that donation; available inventory never increases; donor deferral ledger `+1`; confirmatory result changes the donor to permanent or timed deferral according to the result; all actions are audited.

## 7. Blood Bank Officer patient workflow

Use one released component from LAB-03.

### BBO-02 Create a clinical request

1. Sign in as Dr. Ahmed and open **Clinical Requests**.
2. Record the open-request count and the available count for the intended component/group.
3. Create request `UAT-A-EP-001` for one unit, with an appropriate blood group, `URGENT` urgency, ward and clinical context.
4. Open the request detail.

Expected deltas: open requests `+1`; request status `PENDING`; requested `1`, issued `0`; inventory unchanged; action timeline and audit each show creation once.

### BBO-03 Crossmatch

1. Select the first suitable FEFO candidate from the request detail.
2. Record a compatible crossmatch using a configured method.

Expected deltas: request status `PENDING → CROSSMATCHED`; selected unit `AVAILABLE → CROSSMATCHED`; live available count `-1`; crossmatched count `+1`; issued remains `0`; open requests unchanged.

### BBO-04 Issue with two-identifier handover

1. Issue the crossmatched unit.
2. Enter the exact patient/episode reference and the person collecting custody.
3. First test a wrong patient reference and verify issue is refused with no state change; then submit the correct reference.

Expected deltas after the valid submission: request issued `0 → 1`; request status `CROSSMATCHED → ISSUED`; unit `CROSSMATCHED → ISSUED`; unresolved custody `+1`; available remains unchanged because it was already reduced at crossmatch.

### BBO-05 Record completed transfusion

1. Record outcome `COMPLETED` and reaction `NONE`.
2. Verify custody is closed and the request timeline is complete.

Expected deltas: unit `ISSUED → TRANSFUSED`; unresolved custody `-1`; request status `ISSUED → CLOSED`; open requests `-1`; transfusion records `+1`; units issued remains a historical `1` on the request.

### BBO-06 Return-to-stock exception

Run this with another UAT request/unit:

1. Crossmatch and issue the unit.
2. Return it with cold chain intact and no more than `30` minutes outside controlled storage.
3. Verify the unit returns to available inventory, request units issued decreases, and the request reopens.
4. Repeat with another unit using more than `30` minutes or broken cold chain.

Expected deltas: accepted return gives `ISSUED → AVAILABLE`, available `+1`, request units issued `-1`; rejected return gives `ISSUED → DISCARDED`, discarded `+1`, request units issued `-1`. Both close custody and record the reason.

### BBO-07 Replacement and closure controls

1. Create a request with replacement required `2`.
2. Record one verified replacement receipt; balance decreases by `1`.
3. Waive the remaining `1` with a reason of at least 12 characters.
4. Verify the receipt and waiver appear once in the timeline/audit.
5. Confirm a request with issued units cannot be cancelled; a crossmatched but unissued request can be cancelled and releases its unit back to available stock.

## 8. RBC Coordinator network workflow

### RBC-01 Scope and network intelligence

1. Sign in as Sadia Fatima.
2. Select a facility, then switch among **My facility** and **My RBC network** where available.
3. Open Command Centre, Forecast, Expiry Rescue, Facilities and Analytics.
4. Verify every page changes to the selected scope, never exposes donor or patient identity, and shows data freshness/feed health.

Expected state change: none. Scope switching changes signed-session context only; it must not modify inventory or audit clinical records.

### RBC-02 Data import and reconciliation

1. Open **Data & Integrations** and download an inventory or demand template.
2. Upload a small `UAT-A` CSV containing one valid row, one invalid row and one duplicate source reference.
3. Verify preview/mapping before commit.
4. Commit the accepted row and download/inspect the error report.
5. Resolve the generated reconciliation issue with a note.

Expected deltas: import batches `+1`; valid committed rows `+1`; rejected/quarantined count reflects the bad row; duplicate does not create a second operational record; reconciliation issues `+1` then open issues `-1` on resolution; feed last-sync/status updates; audit entries cover preview/commit/resolution.

### RBC-03 Governed transfer lifecycle

Prerequisite: a generated transfer recommendation with source and destination in the coordinator's allowed scope. If none exists, a Provincial or System Administrator first runs the optimizer.

1. Record the recommendation quantity and the source/destination available counts.
2. Open the recommendation and inspect forecast evidence, compatibility path, reserve-floor check, route time, shelf-life feasibility and the unit manifest.
3. Approve at the source.
4. Enter custodian, courier, vehicle, container, seal and valid departure temperature; dispatch.
5. Print/open the dispatch slip and verify every DIN barcode plus the tracking QR/code.
6. Mark departed.
7. At the destination, mark every manifest unit received and accepted, record intact seal, valid receiving temperature and a compatible storage location.

Expected transitions:

- recommendation: `RECOMMENDED → APPROVED → DISPATCHED → IN_TRANSIT → RECEIVED`;
- approval: source `AVAILABLE -N`, `RESERVED +N`;
- dispatch: no unit-state change;
- departure: `RESERVED -N`, `IN_TRANSIT +N`;
- receipt: `IN_TRANSIT -N`, destination `AVAILABLE +N`, and each unit's current facility/storage changes to the destination;
- transfer pending badge decreases when the recommendation leaves `RECOMMENDED`;
- one audit event exists for each gate.

### RBC-04 Transfer exceptions

Test on separate recommendations:

- Reject: recommendation becomes `REJECTED`; inventory unchanged; structured reason recorded.
- Modify: quantity decreases; retained manifest remains FEFO; inventory unchanged until approval.
- Cancel after approval/dispatch but before departure: `RESERVED → AVAILABLE` for every manifest unit.
- Broken seal or invalid receiving temperature: received units become destination `QUARANTINE`; cold-chain breach count increments for temperature failure; accepted count is zero; transfer becomes failed/quarantined.
- Missing bag: missing unit remains attributed to source and becomes `MISSING_IN_TRANSIT`; arrived units move to destination; receipt is partial.

## 9. Emergency Controller workflow

### EC-01 Preparedness simulation

1. Sign in as Provincial Emergency Cell.
2. Run `UAT-A Bus Accident` in PREPAREDNESS mode with a fixed seed.
3. Record casualties, required units, coverage, projected gap, emergency transfers, donor mobilisation and degraded facilities.
4. Run an intervention comparison with the same seed and one changed control.

Expected deltas: simulation runs `+1`, then comparison runs `+1`; parent/child comparison is linked; inventory status counts are byte-for-byte unchanged; no live alert or transfer approval is created merely by simulation.

### EC-02 Declare live response

1. On the selected simulation, first submit an incorrect acknowledgement and verify refusal.
2. Type exactly `DECLARE LIVE RESPONSE`.
3. Verify one active incident, one emergency transfer plan, zero or more physical FEFO recommendations, and one open surge alert are created.

Expected deltas: active incidents `+1`; transfer plans `+1`; recommended transfers `+N`; open alerts `+1`; available/reserved/in-transit/received inventory counts remain unchanged at declaration.

### EC-03 Execute and resolve

1. Open an emergency recommendation tied to the active incident.
2. Verify the Emergency Controller can approve outbound only while that specific incident is active.
3. Execute approval/dispatch/depart/receive using the same transfer checks as RBC-03.
4. Acknowledge the surge alert, then resolve the incident with a substantive note.

Expected deltas: acknowledgement assigns the alert but keeps it in active workload; incident resolution changes `ACTIVE → RESOLVED`; the incident's surge alert resolves; conditional emergency outbound approval disappears after resolution; completed physical transfers remain in history.

## 10. Provincial Administrator workflow

### PA-01 Province scope and operating picture

1. Sign in as Dr. Tariq.
2. Select **Province** scope.
3. Open Command Centre, Facilities, Analytics, Forecast, Expiry Rescue, Transfers, Alerts and Data.
4. Verify facility total, healthy/degraded feed counts, critical facilities, available units and analytics all use the same province scope.
5. Export Analytics CSV and reconcile its facility row count to the onscreen facility comparison.

Expected state change: none from viewing/export. Scope and facility selectors must not mutate operational data.

### PA-02 Alert accountability

1. Record active-alert count.
2. Acknowledge one alert with a note.
3. Verify status and assignee change but active workload count remains because acknowledged alerts are still unresolved.
4. Resolve it with evidence.

Expected deltas: resolution reduces active-alert count `-1`; occurrence count is preserved; acknowledgement and resolution each add an audit event.

### PA-03 Administrative policy

1. Open Admin.
2. Change one non-system UAT account's role or active status, verify the next sign-in/navigation changes, then restore it.
3. Change one optimizer weight, save, verify persistence and audit, then restore the original value.
4. Run the optimizer.

Expected deltas: user update audit `+1` per change; optimizer-setting audit `+1` per save; optimizer run request audit `+1`; a new plan is generated with `N` recommendations; inventory remains unchanged until a recommendation is approved.

## 11. System Administrator workflow

### SA-01 Prove the clinical boundary

1. Sign in as System Administrator.
2. Verify Admin, Data, Facilities, Analytics, Alerts and network intelligence are available.
3. Attempt to open Donors, Sessions, Lab, Inventory and Clinical Requests directly.

Expected result: clinical/bench pages return a permission boundary and do not show actionable clinical controls. System Admin is a platform administrator, not a clinical super-user.

### SA-02 Users, facilities and audit

1. In Admin, edit and restore a UAT user's access.
2. In Facilities, edit and restore one facility's integration mode, network response SLA and sharing flags.
3. Verify the change is visible on facility detail and one audit event is written per save.
4. Search/inspect the central audit explorer for the UAT run label and actors.

Expected deltas: settings/user values change exactly once per save; audit entries `+1` per action; no inventory change.

### SA-03 Integration/API administration

1. Create a scoped API client.
2. Use its key once against the permitted endpoint and verify facility/organization scope.
3. Revoke it and verify subsequent authentication fails.
4. Confirm the secret is shown only at creation and stored hashed.

Expected deltas: active API clients `+1`, then `-1` active after revocation; audit create/revoke `+1` each; operational inventory changes only if a valid idempotent payload is explicitly committed.

### SA-04 Optimizer governance

1. Record the current optimizer weights and current plan ID.
2. Save a valid change where shortage prevention remains more important than transport cost.
3. Attempt an invalid weight set and verify it is refused without persistence.
4. Run the optimizer and wait for the background job.
5. Open Transfer Plans and verify a newer plan ID and recommendation count.

Expected deltas: valid setting save audit `+1`; invalid save `0`; optimizer request audit `+1`; transfer plans `+1`; recommendations `+N`; inventory unchanged.

### SA-05 Guided network onboarding and first access

Run this on the resettable UAT database with a unique label such as `UAT-A-DHQ`.

1. Open **Admin → Network onboarding** and create a new hospital-group organization and facility with realistic synthetic geography, capability, storage, feed, reserve and first-account details.
2. Verify the result is an inactive draft, the organization/facility/account are excluded from every operational selector, and the review page reports identity, regular storage, quarantine, reserve policy, connection, accountable access and network relationship separately.
3. Verify activation is unavailable until all seven checks pass; add any intentionally omitted storage prerequisite through the product and recheck.
4. Explicitly activate the facility. Verify the organization, facility and prepared account become active in one action, the facility opens in a truthful no-data state, one activation audit event exists and intelligence becomes pending.
5. Sign in as the prepared account with its temporary password. Verify every operational route redirects to password replacement until a strong new password is saved; the temporary password then fails and the new password reaches only the assigned role/facility.

Expected deltas: drafts `+1` at creation then `-1` at activation; active organizations/facilities/users each `+1`; available inventory and demand remain unchanged at zero for the new facility; onboarding and activation audits each `+1`; intelligence source version `+1` only on activation, then returns clean after refresh.

## 12. Operating-model validation

### MODE-01 Standalone opted out

1. Sign in as Ayesha Hussain at Shaukat Khanum.
2. Verify donor, lab, inventory and request workflows are available locally.
3. Verify the facility is marked not publishing to the network.
4. Verify cross-organization transfer approval cannot use this facility while sharing consent is off.

### MODE-02 Standalone opted in

1. Sign in as Dr. Zainab at Children's Hospital.
2. Verify the same local workflow remains facility-contained.
3. Verify safe inventory/feed signals participate in eligible network planning because both organization and facility consent are enabled.
4. Verify donor and patient identity never appear on network pages.

### MODE-03 RBC hub-and-spoke network

1. Sign in as Dr. Bilal Khan at RBC Lahore.
2. Compare **My facility** with **My RBC network**.
3. Verify spokes appear in the network scope and unit-level movement still requires source approval, dispatch custody and destination receipt.

### MODE-04 Province

1. Sign in as Dr. Tariq or the Emergency Controller and select Province.
2. Verify aggregate risk, feed health, analytics and planning span Punjab facilities allowed by role.
3. Verify a province view does not erase organization ownership, facility custody, sharing consent, reserve floors, compatibility, cold-chain or human approval.

## 13. Defect report format

Use this exact structure when something is wrong:

```text
Test: BBO-04
Role/account: Blood Bank Officer / dr.ahmed@...
Facility and scope: Jinnah Lahore / My facility
Record: request code and DIN
Before: request=CROSSMATCHED, issued=0, available PRBC O+=41
Action: issued unit after correct two-identifier confirmation
Expected: request=ISSUED, issued=1, unit=ISSUED, available unchanged from post-crossmatch
Actual: ...
Page/URL: ...
Screenshot: ...
```

This gives enough information to reproduce the state transition without guessing which facility, scope, record or counter was involved.
