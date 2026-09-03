# Sprint 14 — Role-first UX and demonstration-data recalibration

**Release:** 0.14.0  
**Status:** Implemented; validation evidence is generated during promotion.

## Outcome

The platform remains functionally complete, but no longer presents every
capability at once. Each role opens with a three-item **My work** area; secondary
capabilities remain available in labelled, collapsed groups. Dense dashboard
inventory evidence is progressively disclosed and system diagnostics no longer
compete with daily work in the sidebar.

The former oversized demonstration dataset is replaced through the existing
deterministic rebuild pipeline. Organizations, facilities, roles, policies and
demo identities stay stable, while transactional synthetic records are rebuilt
from seed using a facility-scale profile.

## Data profile

- 180–320 seeded donors per facility, scaled by facility type; recent operations
  may add donors but the validator caps the final register at three times its
  type target.
- Five days of detailed operational workflow and seven days of unit-level
  inventory, while 547 days of aggregated demand and inventory snapshots remain
  available for defensible forecasting and backtesting.
- Collection, screening, compatibility substitution, shortage, deliberate
  over-ordering, expiry and FEFO behavior are consequences of the simulation,
  not arbitrary page fixtures.
- Calibrated seeded outcome: **11.4% wastage**, **98% expiry-driven wastage**,
  **3.4% unmet demand**, **96.3% fill rate**.
- A persisted `synthetic.dataset_profile` record proves which generator profile
  and measured outcomes produced the database.

## Exit gates

- Role-first navigation has exactly three non-duplicated primary actions for
  every role.
- Secondary navigation and dense dashboard detail use accessible progressive
  disclosure in English and Urdu/RTL.
- `python -m scripts.validate_synthetic_dataset` passes global realism,
  facility-distribution, storage-capacity, lifecycle, scope and foreign-key
  checks.
- Forecast acceptance, workflow regression, release acceptance and bilingual
  desktop/mobile browser checks pass before the database is promoted.
