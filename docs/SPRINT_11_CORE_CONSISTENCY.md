# Sprint 11 — Core Operational Consistency

**Owner:** Faheel + Codex  
**Delivery shape:** one intensive day (approximately two normal workdays)  
**Priority:** core functionality before further presentation features  
**Status:** Complete — 28 August 2026

## Outcome

An authorized operational action changes the clinical record, physical stock,
canonical demand, network risk, expiry rescue, KPIs and dashboards as one
understandable system. Users can see whether decision data is current, updating
or failed, and an administrator can retry safely without changing clinical
records.

## Scope and work order

1. Link each local clinical blood request to exactly one identity-free
   `DemandEvent`; keep requested, issued, urgency, substitution and final
   outcome synchronized through the request lifecycle.
2. Invalidate the decision snapshot in the same transaction as every audited
   request, donation, processing, laboratory, inventory, issue, transfer,
   emergency and committed-integration change.
3. Rebuild demand marts, shortage risk, expiry rescue, days of cover, facility
   KPIs and impact as one versioned transaction. A write that arrives during a
   rebuild must remain pending for the next run.
4. Run the coalesced refresh worker automatically and expose current, updating,
   pending and failed status in the shared application shell.
5. Give authorized administrators a clearly labelled, audited manual retry.
6. Validate lifecycle synchronization, failure/retry recovery, concurrent
   invalidation, permissions, bilingual copy, schema migration and the full
   regression suite against a disposable copy of the synthetic database.

## Product decisions

- The trained forecast remains scheduled. A new same-day request updates live
  demand, risk and coverage; it does not retrain a model on a partial day.
- Refreshes coalesce for up to five seconds by default, keeping form workflows
  fast while bringing dashboards current well inside a one-minute target.
- Clinical truth is never rolled back because an analytical refresh fails.
  Failure is durable and visible, and retry rebuilds derived data from source
  records.
- Cancelled requests remain auditable in clinical history but are excluded
  from analytical demand totals.
- Unknown patient blood groups are not assigned to an invented analytical
  group. They begin contributing when the group is recorded.

## Exit gates

- One request creates one demand event and cannot double-count on edit or
  retry.
- Issue, return, transfusion, not-returned and cancellation paths leave request,
  unit, demand and audit data consistent.
- Relevant operational writes increment the refresh version in their own
  transaction; unrelated administrative reads/writes do not cause refresh
  loops.
- A successful run marks the exact processed version current; a mid-run write
  leaves the state pending; a failed run records a safe error and can recover.
- Footer and administration surfaces are usable in English and Urdu and obey
  role permissions.
- Migration, focused tests and the complete repository test suite pass against
  a disposable database. The original demonstration database is preserved.

## Validation result

- Additive migration creates one refresh-state table and one request link, then
  reports a clean schema check.
- Ordinary full-data operational refresh: **18.9 seconds**.
- Deliberate full recovery rebuild: **49.2 seconds** for 714,685 demand rows,
  20,160 risk rows, 3,590 rescue rows and all 30 facility KPIs.
- Complete suite: **550 passed, 3 intentional skips**.
- English and Urdu/RTL desktop and 390×844 checks pass without horizontal
  overflow, untranslated tokens, unsafe error text or browser-console warnings.
