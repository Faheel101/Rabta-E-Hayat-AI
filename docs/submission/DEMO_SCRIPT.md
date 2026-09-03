# Four-minute judging demonstration

## Before entering the room

1. Start the Docker build early and confirm `/health/live` and `/health/ready` return HTTP 200.
2. Open the application at 1280 × 720 or larger and keep a second tab on the public GitHub README.
3. Sign in as the RBC Coordinator and open `/showcase`.
4. Keep the Qwen key state truthful. If it is not configured, explicitly show the verified deterministic-fallback label.
5. Do not regenerate data, approve a transfer or declare an emergency immediately before judging unless that action is part of a rehearsed resettable demo.

## 0:00–0:35 — the problem and the system

“Blood is perishable, demand is uncertain, and stock is fragmented across facilities. Rabta-e-Hayat connects the entire blood-bank workflow to a governed sharing network, so a shortage and a near-expiry surplus become one accountable decision.”

Show the guided showcase and the four operating layers: facility, standalone organization, RBC/network and province.

## 0:35–1:20 — prove the operational foundation

Open the facility dashboard, then briefly follow one DIN through collection, laboratory testing, component processing and available inventory.

Say: “This is not a forecasting dashboard floating above mock stock. Every prediction and recommendation resolves to named physical units with test, storage and custody history.”

Point out quarantine, independent release, compatibility and role boundaries. Do not spend time completing a full collection transaction unless the panel asks.

## 1:20–2:10 — show prediction becoming action

Open the Command Centre and Demand Forecast.

Show:

- P10/P50/P90 demand rather than a single false-precision number;
- the reserve-floor and stockout projection;
- model/backtest/baseline labels and data freshness;
- one high-risk facility/component/group series.

Then open Expiry Rescue. Explain the four visible steps: identify risk, review evidence, make the human decision, execute custody. Open a **Review and decide** record.

## 2:10–3:05 — prove the governed transfer

On the transfer record, show the named DIN manifest, source/destination, compatibility path, counterfactual impact, travel and cold-chain envelope.

Open the Approve, Modify and Reject choices without submitting them.

Say: “The optimizer can recommend. It cannot move one bag. Approval reserves named units; dispatch changes custody; receipt reconciles each unit into available, quarantine or missing.”

## 3:05–3:35 — show AI without overstating it

Open Ask Rabta AI or the inline transfer explanation.

Say: “Qwen explains validated facts through a governed gateway. Direct identities and unit IDs are blocked, every number must trace to the source facts, and invalid or unavailable output falls back deterministically. AI never approves clinical or inventory actions.”

If the API is not connected, point to the fallback label and say that this is deliberate failure-safe behavior.

## 3:35–4:00 — finish on system-level value

Open the Emergency Simulator or Data & Integrations for one final proof point: a reproducible scenario comparison or a quarantined/imported record with provenance.

Close with:

“Rabta-e-Hayat gives each hospital a complete blood-bank system and gives the network something it does not have today: earlier warning, feasible redistribution, and an audit trail from prediction to patient-facing custody.”

## Likely panel questions

- **Is this real patient data?** No. The release contains realistic deterministic synthetic data only.
- **Does AI decide where blood goes?** No. Statistical models estimate demand, CP-SAT finds feasible proposals, and authorized humans approve and execute every movement.
- **What happens without internet or Qwen?** Core operations, forecasting, optimization and safety controls continue; AI explanations use a visibly labelled deterministic fallback.
- **How do hospitals connect?** Through normalized CSV, FHIR R4, HL7 v2 or API feeds with archive, validation, quarantine, provenance, idempotency and reconciliation.
- **Can one hospital see another hospital's donor or patient identity?** No. Organization tenancy, facility custody and sharing consent remain enforced boundaries.
- **What is needed for production?** Clinical and regulatory validation, privacy/security review, native Urdu review, production infrastructure and live pilot integrations.
