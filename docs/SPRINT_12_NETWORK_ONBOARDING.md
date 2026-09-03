# Sprint 12 — Network Onboarding and Operational Administration

**Owner:** Faheel + Codex  
**Status:** Complete — 29 August 2026  
**Release:** 0.12.0

## Outcome

Rabta-e-Hayat can now add a standalone blood bank, hospital group facility,
RBC hub/spoke or provincial-programme facility through the bilingual product.
The workflow creates a safe inactive draft first. No new facility, account,
inventory signal, forecast or network availability becomes operational until an
authorized system administrator passes the persisted readiness gate and makes
an explicit activation decision.

## Implemented contract

1. **Network identity:** create a tenant or reuse an active organization, with a
   declared operating mode and privacy posture.
2. **Facility profile:** validate code, geography, capability, parent-RBC
   relationship, bed count and response SLA.
3. **Operational foundation:** create regular and quarantine storage, a
   registered integration feed, component capacity, operating hours and an
   eight-blood-group reserve floor from the same policy used by seeded sites.
4. **Accountable access:** prepare an inactive facility officer or coordinator
   with a strong temporary credential.
5. **Readiness:** query identity, storage, quarantine, reserve policy, feed,
   access and network relationship from the database on every review and again
   immediately before activation.
6. **Activation:** atomically activate the tenant/facility/prepared accounts,
   record who activated it, audit the decision and dirty the decision snapshot.
   The refresh builds truthful zero/no-data states; it never fabricates stock or
   demand.
7. **Central user administration:** create a scoped account, assign role and
   facility, deactivate safely, reset a temporary password, revoke live
   sessions and require the user to replace that password before any operational
   dependency resolves.

## Safety and authorization decisions

- `MANAGE_NETWORK` is intentionally limited to the system-administrator role;
  provincial and tenant administrators cannot create a new security boundary.
- Standalone mode forcibly disables network inventory/contact sharing.
- RBC spokes require an active parent RBC in the same province.
- Draft creation is one audited transaction; invalid input leaves no orphaned
  organization, facility, storage, feed or user.
- Existing active facilities remain backward-compatible: a null onboarding
  status is treated as active and no historical activation metadata is invented.
- Operational roles must be pinned to an active facility in their organization.
  Cross-tenant assignment and role escalation fail closed.
- The final system administrator cannot be removed, and an administrator cannot
  deactivate or demote their own active session.

## Validation result

- Additive migration preview identified exactly three facility columns:
  `onboarding_status`, `activated_at` and `activated_by`; apply followed by
  `--check` reported a clean schema.
- Release readiness reported configuration, database schema, assets and SQLite
  integrity all healthy for version 0.12.0.
- Focused onboarding, centralized-user, authorization, rollback, password-gate,
  translation and web-flow tests pass.
- Complete repository suite: **565 passed, 3 intentional skips**.
- Browser journey: system administrator created a realistic synthetic hospital
  group and facility, reviewed a **7/7** readiness gate, activated it, and opened
  its truthful no-activity facility view.
- English and Urdu/RTL checks pass at the default viewport and 390×844 with no
  document overflow, untranslated tokens or browser-console warnings.
