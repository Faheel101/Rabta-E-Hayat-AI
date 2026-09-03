# Rabta-e-Hayat Operating Model

Rabta-e-Hayat is one integrated platform with four operating views, not four disconnected products. The same facility, unit, request, forecast and transfer records are reused; role, organization ownership, sharing consent and selected scope decide what each user can see and do.

## 1. The four layers

### A. Facility operations

This is the local blood bank management system for one physical site.

It owns the vein-to-vein chain:

`Donor → screening → collection → quarantine → component separation → TTI testing → independent release → available inventory → request → crossmatch → issue → transfusion/return/discard`

Characteristics:

- donor and patient/episode records remain local to the owning organization/facility;
- every physical unit has a DIN, current facility, component, group, expiry, screening state, storage/cold-chain state and custody history;
- operational counters are live from transactional tables;
- phlebotomy, laboratory and issue permissions are separated;
- facility work can continue even when the facility does not participate in the wider sharing network.

### B. Standalone organization

A standalone hospital is an organization with one facility. It uses the same facility operations as every other participant but has no internal facility-switching requirement.

There are two valid standalone states:

1. **Standalone, network opted out** — local donor/lab/inventory/request operations work normally; its stock is not eligible for cross-organization planning or transfer.
2. **Standalone, network opted in** — local operations remain private, but safe supply signals and permitted unit evidence can participate in network planning. Physical movement still needs source approval and destination receipt.

Standalone does not mean isolated software. It means one legal/operational organization owns one blood bank and decides whether it shares inventory with other organizations.

### C. RBC or organization network

This is the collaboration layer across facilities belonging to a hospital group or an RBC hub-and-spoke network, plus consenting cross-organization partners.

It consumes normalized data from the facility layer and provides:

- command-centre balance and data freshness;
- facility/component/group demand forecasts;
- shortage risk and days of cover;
- expiry-rescue opportunities;
- optimized FEFO transfer recommendations;
- dispatch, tracking, receipt and exception custody;
- network alerts, integration health and reconciliation.

Network rules:

- donor identity and patient identity are not shared;
- an organization and both facilities must retain sharing consent for a cross-organization transfer;
- the source reserve floor, compatibility, unit release state, expiry window, cold-chain history and route feasibility are rechecked at approval time;
- an optimizer recommendation changes no inventory;
- source approval reserves the selected physical DINs;
- departure and receipt update actual unit custody once, with an audit event at every gate.

### D. Province

Province is the widest health-system operating view for the Provincial Administrator and Emergency Controller. It is an oversight and coordinated-response scope, not a new owner of every facility's records.

It provides:

- province-wide facilities, feeds, shortage/expiry risk and impact analytics;
- facility comparison and CSV export;
- alert acknowledgement/escalation/resolution;
- optimizer policy and province planning for authorized administrators;
- emergency preparedness simulation, intervention comparison and live incident declaration;
- governed emergency transfer recommendations.

Province scope does not bypass:

- organization tenancy;
- facility custody;
- network opt-in;
- local reserve floors (except an explicit, audited emergency reserve policy);
- compatibility and shelf-life rules;
- cold-chain limits;
- human approval.

## 2. Organizational types in the seeded MVP

| Organization type | Meaning | Seeded example |
|---|---|---|
| `STANDALONE_HOSPITAL` | One organization, one hospital blood bank | Children's Hospital; Shaukat Khanum |
| `HOSPITAL_GROUP` | One organization, several hospital blood banks | Punjab Teaching Hospitals Group |
| `RBC_OPERATOR` | Regional Blood Centre plus hub-and-spoke facilities | RBC Lahore; RBC Multan |
| `GOVT_PROGRAMME` | Government oversight/operator group | South Punjab District Health Authority |

The organization is the tenancy and accountability boundary. A facility is the physical stock/custody boundary. Scope is a viewing/planning boundary. These three concepts must not be treated as interchangeable.

## 3. Scope selector behavior

The selected scope changes aggregate pages but does not rewrite records.

| Scope | Includes | Typical users |
|---|---|---|
| My facility | Active physical blood bank only | Phlebotomist, Lab Technologist, BBO |
| My RBC network | Hub and its configured spokes/group facilities | BBO, RBC Coordinator |
| District | Facilities in the active facility's district | Higher-scope planning roles |
| Division | Facilities in the active facility's division | Provincial/Emergency planning |
| Province | Facilities in the same province | Provincial Administrator, Emergency Controller |
| All | All configured facilities | System Administrator |

The facility selector answers **where am I operating?** The scope selector answers **how much of the network am I analysing?** A group-level user selects an active facility when a facility-specific operational action is required.

## 4. Data movement into the platform

Every facility integration mode normalizes into the same records and validation rules:

1. FHIR R4;
2. HL7 v2;
3. proprietary REST/API;
4. SFTP/CSV feed;
5. manual CSV upload;
6. simulated synthetic feed.

The workflow is:

`Source → archive/provenance → map → validate → preview/quarantine → human commit → canonical inventory/demand records → feed health → intelligence pipeline`

Important behavior:

- repeated source references are idempotent and must not double-count;
- invalid or anomalous rows are rejected/quarantined rather than silently accepted;
- reconciliation issues remain open until an accountable user resolves them;
- a stale feed remains visible and degraded instead of disappearing from planning.

## 5. Intelligence and execution are deliberately separate

The platform has two data tempos:

### Transactional tempo

Donor, donation, unit, test, request, crossmatch, issue, transfusion, transfer, alert and audit states update immediately.

### Intelligence tempo

Forecasts, days of cover, shortage risk, expiry rescue, facility KPIs and impact analytics are calculated snapshots. The scheduled flow is conceptually:

`ingest → validate → build demand series → forecast → shortage/expiry risk → optimizer → narratives/alerts → analytical marts`

The UI must always show data freshness. A clinical action is authoritative immediately; a derived KPI becomes authoritative after its snapshot is recalculated.

## 6. Core safety gates

No operating model may bypass these gates:

1. Donor eligibility and deferral ledger.
2. Quarantine until the TTI panel is complete.
3. Independent second-person lab release.
4. Component separation window and loss attribution.
5. Available/PASSED/non-expired/no-breach inventory eligibility.
6. ABO/Rh/component compatibility and FEFO candidate ranking.
7. Valid crossmatch or explicit governed emergency release.
8. Two-identifier issue handover and custody outcome.
9. Source reserve floor and network sharing consent.
10. Cold-chain/route/shelf-life feasibility.
11. Human transfer approval, dispatch custody and destination receipt.
12. Append-only audit evidence for every state-changing gate.

## 7. What changes when scope changes

| Capability | Facility | Standalone | RBC/network | Province |
|---|---|---|---|---|
| Donor/lab/patient operations | Local | Local | Local facility only | Only after selecting an authorized facility/clinical role |
| Unit-level inventory | Local authorized users | Local | Governed movement evidence | Governed evidence; not general donor/patient data |
| Forecast/risk/expiry | Active facility | Single site | Selected network | Selected province |
| Transfer recommendation | Local inbound/outbound | Only if sharing permits | Across eligible network | Across eligible province scope |
| Approval | Local authorized source | Local source | Authorized network source | Authorized province/emergency source |
| Analytics | Facility | Single site | Aggregated network | Province comparison |
| Emergency simulation | View/run by permission | Can be included as a facility | Network surge | Province response |
| Data ownership | Organization + facility | One organization/facility | Ownership stays with contributors | Ownership stays with contributors |

## 8. One-sentence mental model

**Facilities create and consume blood; organizations own and govern the records; networks share safe supply signals and execute consented transfers; the province sees and coordinates the system without erasing local custody or human approval.**
