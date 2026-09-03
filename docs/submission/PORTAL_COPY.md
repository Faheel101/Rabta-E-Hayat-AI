# Alibaba Cloud AI Hackathon Pakistan 2026 — portal copy

Replace only the bracketed identity and URL fields after the portal requirements are confirmed.

## Project name

Rabta-e-Hayat

## One-line description

A governed blood-supply operating network that connects hospital blood-bank workflows with demand forecasting, expiry rescue, constrained redistribution, emergency simulation and accountable human decisions.

## Project summary

Rabta-e-Hayat is a bilingual blood-supply operating network for hospitals, regional blood centres, hospital groups and provincial health teams. It manages the complete vein-to-vein workflow—from donor eligibility, collection, testing and component release through inventory, clinical requests, crossmatching, transfusion, returns and disposal. Above this foundation, it adds probabilistic demand forecasting, shortage detection, unit-level expiry rescue, constrained transfer planning, emergency simulation and named-unit custody with accountable human approval. The working synthetic MVP spans 30 facilities and 36,899 blood units and passes 603 automated tests plus 169/169 live role and permission probes. A governed Qwen/DashScope gateway can explain validated operational facts without changing clinical state; the submission uses its clearly labelled deterministic fallback because no external API key is configured.

## Problem statement and proposed solution

Pakistan's blood supply is perishable and fragmented: one hospital can face a shortage while compatible units approach expiry elsewhere, and teams often coordinate through delayed reports and manual calls. This affects donors, blood-bank staff, clinicians, hospital networks and patients who depend on timely compatible blood. Rabta-e-Hayat combines complete facility blood-bank operations with predictive demand and expiry intelligence, then turns validated risks into feasible network transfer recommendations governed by consent, compatibility, reserve, cold-chain, custody and human approval.

## Detailed project description

Rabta-e-Hayat manages the end-to-end vein-to-vein workflow: donor registration and eligibility, collection sessions, component separation, transfusion-transmissible-infection testing, independent release, storage, inventory, clinical requests, crossmatch, issue, transfusion, returns and disposal. Above this operational foundation, it produces facility/component/blood-group demand forecasts, shortage and expiry risk, constrained FEFO transfer plans, dispatch and receipt custody, operational alerts, impact analytics and emergency simulations. A user starts within a role-specific facility workspace; every action updates the operational record and audit trail, while the versioned intelligence pipeline refreshes forecasts and network decisions. Recommendations never move blood automatically: an authorized human reviews evidence, approves/modifies/rejects, records dispatch custody, and the destination reconciles each received unit.

## Technical approach and technologies

The application uses FastAPI, Jinja, Tailwind CSS and Alpine.js for a responsive bilingual web experience; SQLAlchemy with SQLite for the portable demonstration and PostgreSQL compatibility for deployment; LightGBM quantile models for P10/P50/P90 demand; TSB and seasonal-naive fallbacks for sparse series; OR-Tools CP-SAT for constrained transfers; and reproducible Monte Carlo emergency simulation. CSV, FHIR R4 and HL7 v2 adapters share validation, provenance, idempotency, quarantine and reconciliation controls. Qwen is integrated through DashScope's OpenAI-compatible API behind a provider-neutral governance gateway. It explains only validated scoped facts, while schema, privacy, numeric traceability, timeout, budget, circuit-breaker and deterministic-fallback controls keep the platform functional without an API key. Docker Compose provides a reproducible one-command build.

## Innovation and impact

The innovation is not a chatbot attached to inventory. Rabta-e-Hayat links four normally separate systems—blood-bank operations, probabilistic demand intelligence, constrained network optimization and physical custody—while preserving local ownership and accountable human authority. It can help teams reduce avoidable expiry, identify shortages earlier, use compatible stock more intelligently, coordinate emergencies and prove exactly why and how each unit moved.

## Feasibility and what is built

The integrated synthetic MVP is working across standalone hospitals, hospital groups, RBC hub-and-spoke networks and province oversight. It includes seven permissioned roles, native English/Urdu interfaces, responsive onboarding, realistic deterministic synthetic data, auditable transfer execution, health/readiness endpoints, Docker deployment and comprehensive automated release gates. The candidate passes 603 automated tests and 169/169 live role/permission probes. Qwen connectivity is optional for judging; without a key the same AI surfaces display a verified deterministic fallback rather than pretending an external model responded.

## Delivery plan to close of build phase

The integrated product, synthetic dataset, UI, forecasting, risk engines, transfer optimizer, governed AI gateway, emergency simulator, interoperability layer and release validation are complete. Before submission, the sole developer and Codex will finalize the public repository, verify that no secret or local database is committed, run a clean Docker build, complete the judging deck and demo script, and submit the repository/presentation links. Post-hackathon work is formal clinical validation, native-speaker Urdu review, security/privacy assessment, regulatory mapping, production infrastructure and live pilot integrations.

## Links and identity

- Public repository: `[GITHUB_URL_AFTER_PUSH]`
- Demonstration link or instructions: `[DEMO_URL_OR_LOCAL_DOCKER]`
- Presentation: `[PRESENTATION_URL_OR_UPLOAD]`
- Team lead: `Faheel Anjum`
- Team name: `Rabta-e-Hayat AI`
- Contact: `faheelanjum12@gmail.com`

## Accuracy disclosure

This release uses realistic synthetic data only and is not certified for clinical use. AI produces explanations and suggestions, not clinical or custody decisions.
