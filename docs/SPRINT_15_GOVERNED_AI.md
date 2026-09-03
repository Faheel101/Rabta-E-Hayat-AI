# Sprint 15 — Governed AI Decision Copilot

## Objective

Make the use of AI visible, useful, measurable, and safe across Rabta-e-Hayat
without allowing a probabilistic model to bypass clinical rules, inventory
state machines, custody controls, tenant scope, or accountable approval.

Rabta already contains two forms of decision intelligence:

- machine-learning demand forecasting using quantile LightGBM, with TSB and
  baseline fallbacks for sparse series;
- constraint-based transfer optimization using OR-Tools CP-SAT.

The missing layer is a governed generative-AI gateway and the outcome-learning
loop around those engines. Existing deterministic narratives are retained as
the mandatory offline fallback.

## Non-negotiable authority boundary

AI may:

- explain facts and recommendations already computed by trusted services;
- summarize changes, risks, alerts, and scenario comparisons;
- rank attention and propose next actions;
- detect unusual data or performance patterns;
- propose optimizer or alert parameter changes for human review;
- draft donor communications from approved, non-clinical campaign facts.

AI may never:

- decide donor eligibility, compatibility, testing, release, issue, or
  transfusion disposition;
- fabricate or calculate a forecast, shortage quantity, reserve floor, blood
  group compatibility path, route feasibility, or unit manifest;
- approve, dispatch, receive, discard, or otherwise mutate a blood unit;
- activate a facility, alter permissions, or change a policy;
- send donor or patient identifiers to an external model;
- silently replace a validated deterministic result.

Every operational mutation remains a typed, permissioned, audited human action.

## Product capability map

### 1. AI command-centre brief

- Generate concise English and Urdu facility/network briefings from a resolved
  fact object.
- State what changed since the previous refresh, the three most important
  actions, their deadlines, and the evidence behind each action.
- Show provider/model, generation time, source freshness, fact validation, and
  whether deterministic fallback was used.

### 2. Recommendation explanations

- Explain forecasts, expiry rescue, transfers, alerts, and emergency results in
  role-appropriate language.
- Answer bounded follow-up questions only from the current structured evidence.
- Link every number and named facility to a source field.
- Never answer outside the user's organization/facility scope.

### 3. Forecast guardian

- Monitor data freshness, drift, interval coverage, baseline performance,
  fallback rate, and abnormal forecast changes.
- Produce a diagnostic explanation and recommend retraining, fallback, or
  review; it does not publish a new model by itself.
- Keep the numerical forecast in the existing statistical/ML engine.

### 4. Transfer-plan copilot

- Explain why the solver selected a route, why alternatives were rejected, and
  the counterfactual shortage/waste impact.
- Learn from structured rejection reasons, delays, discrepancies, cold-chain
  exceptions, and realized outcomes.
- Propose optimizer weight changes in a sandbox; an authorized administrator
  must compare, approve, and publish them.
- The CP-SAT constraints remain the final validity boundary.

### 5. Inventory and feed anomaly monitor

- Detect unusual demand, collection, wastage, temperature, stock, and import
  patterns using deterministic anomaly scores or trained detectors.
- Use generative AI only to summarize validated signals and propose an
  investigation checklist.
- Quarantine suspicious imports through existing rules; AI cannot commit them.

### 6. Emergency response copilot

- Compare simulation runs, explain changed assumptions, summarize gaps and
  recommended mobilization, and draft bilingual incident briefs.
- All numbers continue to come from the Monte Carlo simulator and transfer
  optimizer.

### 7. Donor engagement assistant

- Recommend campaign segments using eligibility date, geography, blood-group
  scarcity, response history, and communication consent.
- Draft bilingual outreach variants for human approval.
- Exclude medical history, identifiers, and free-text clinical notes from model
  prompts; never override the eligibility engine.

### 8. Integration copilot

- Explain quarantined rows and suggest source-to-canonical field mappings.
- Require a human preview and commit through the existing import workflow.
- Treat all imported text as untrusted data, never as model instructions.

## Shared AI gateway

One provider-neutral service will be used by every capability. It will enforce:

- feature-level enable/disable switches;
- an explicit allowlist of prompt fields for each use case;
- pseudonymization and rejection of patient/donor identifiers;
- tenant and facility scope before prompt construction;
- structured JSON output with schema validation;
- source-number and named-entity validation;
- timeout, one bounded retry, circuit breaker, and deterministic fallback;
- low temperature for factual operations;
- maximum input/output size, latency, and per-feature cost budgets;
- prompt-injection delimiters for imported or user-authored text;
- immutable audit metadata: feature, prompt version, provider, model, source
  hash, actor, scope, latency, usage, validation result, and fallback reason;
- no storage of secrets, raw prompts containing identifiers, or provider
  credentials in the database or logs.

## Visible trust contract

Every AI-assisted surface will show one of four states:

- **AI generated · verified against source facts**
- **AI suggestion · human approval required**
- **AI unavailable · verified deterministic fallback**
- **AI output blocked · validation failed**

Users can open an evidence drawer showing the source facts, model metadata,
freshness, limitations, and the human action required. Marketing labels are not
evidence; the live status and audit record are.

## Frozen implementation decisions

- Provider: Qwen through DashScope's OpenAI-compatible chat-completions API.
- Default model: `qwen3.7-plus`, configurable without a code change.
- Demonstration connectivity: optional. Every feature has a validated,
  deterministic offline response and the UI labels that state truthfully.
- Provider timeout: 8 seconds, with at most one controlled retry.
- Demonstration budget guardrail: 250,000 tokens and USD 20 estimated cost per
  organization per day; provider billing remains authoritative.
- Context policy: operational facility names, routes, metrics, blood groups,
  scenario parameters and risks are allowed when already in the user's scope.
  Direct identities, contacts, clinical free text, credentials, unit IDs and
  donation IDs are blocked.
- Authority: automatic read-only briefs, explanations, alerts and suggestions
  are allowed; every clinical, custody, inventory and policy mutation remains a
  permissioned human action.
- Product placement: both inline assistance and a constrained **Ask Rabta AI**
  workspace are delivered.

## Delivered in release 0.15.0

1. One governed Qwen gateway with JSON schema validation, numeral/facility
   traceability checks, prompt-injection containment, privacy filtering,
   feature switches, size limits, timeout, retry, circuit breaker, cache and
   per-tenant budget controls.
2. Bilingual Ask Rabta AI workspace using role-, organization- and
   facility-scoped operational facts.
3. Inline command-centre briefing, forecast-quality guardian, transfer
   rationale and emergency-scenario briefing.
4. Optimizer policy adviser using scoped transfer outcomes, rejection feedback
   and current configured weights, without the ability to save a weight or run
   the optimizer.
5. Privacy-minimised interaction audit table and administrator control centre
   for health, model, cost, latency, validation and fallback state.
6. Deterministic offline answers for every capability, kept available whether
   the API key, internet or provider is absent.
7. Adversarial automated coverage for identity blocking, output fabrication,
   timeout, retry, tenant-scoped caching, non-mutation, RBAC and bilingual UI.

Donor outreach drafting and import-field mapping remain deliberately outside
this release. They are useful extensions, but they are not allowed to widen the
final MVP's external data boundary or introduce messaging/import side effects
without a dedicated consent and approval sprint.

## Delivery sequence

### Phase A — safety and observability foundation

1. Provider-neutral gateway, feature policy, redaction, schemas, validation,
   timeouts, retries, circuit breaker, and offline fallback.
2. AI interaction audit table and an administrator health/cost workspace.
3. Contract, redaction, injection, timeout, tenancy, and fallback tests.

### Phase B — visible MVP value

1. AI facility/network morning brief in English and Urdu.
2. AI transfer rationale and bounded evidence Q&A.
3. AI emergency incident brief and scenario comparison.
4. Visible AI/fallback badges and evidence drawers.

### Phase C — monitored learning

1. Forecast guardian and anomaly explanations.
2. Optimizer outcome dataset and weight-change proposals in a sandbox.
3. Donor campaign segmentation and approved-message drafting.
4. Import mapping suggestions and quarantine explanations.

## Acceptance gates

- The application is fully usable with the provider disabled or unreachable.
- AI output changes no clinical or inventory state directly.
- No prompt contains donor/patient names, CNICs, phone numbers, email addresses,
  medical notes, unit identifiers, or other disallowed fields.
- Every number and facility named in prose is present in the source facts.
- Invalid, invented, overlong, cross-tenant, or schema-breaking output is
  blocked and falls back safely.
- English and Urdu are generated independently from the same fact object.
- Repeated identical source facts can be cached without crossing tenant scope.
- Administrators can see provider health, failures, validation blocks, latency,
  usage, estimated cost, prompt version, and fallback rate.
- Tests cover success, provider outage, malformed output, prompt injection,
  sensitive-data leakage, timeout, retry exhaustion, and tenant isolation.
- Browser QA covers desktop/mobile and English/Urdu for every AI state.

## Connection setup

Set `QWEN_API_KEY` in the runtime environment to enable verified Qwen calls.
Leave it empty to rehearse the complete platform in offline-safe mode. Never put
the key in source control, a prompt, a URL or an audit record.
