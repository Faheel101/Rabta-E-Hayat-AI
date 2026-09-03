# Project summary

Rabta-e-Hayat is a governed blood-supply operating network built to help hospitals, regional blood centres, hospital groups and provincial health teams manage blood from donor registration through transfusion while anticipating shortages and rescuing usable units before expiry.

The platform serves blood-bank officers, phlebotomists, laboratory teams, network coordinators, emergency controllers and health administrators. Each role receives a focused workspace for the actions it is authorized to perform, with English and Urdu interfaces, guided workflows and a complete audit trail.

We built the complete synthetic demonstration MVP: donor eligibility and deferrals, collection sessions, component processing, transfusion-transmissible-infection testing, independent release, storage and inventory, clinical requests, crossmatching, issue, transfusion, returns and disposal. Above those core workflows, Rabta-e-Hayat adds P10/P50/P90 demand forecasting, shortage detection, unit-level expiry rescue, OR-Tools constrained transfer recommendations, human approval, named-unit dispatch and receipt custody, emergency simulation, alerts, analytics and CSV/FHIR/HL7 integration controls.

Qwen is integrated through a governed Alibaba Cloud DashScope gateway for operational explanations and briefs. It can use only validated, scoped facts and cannot alter clinical or custody records. The submission runs with a clearly labelled deterministic fallback because no external API key is configured.

The result is not simply an inventory system or chatbot. It is an integrated decision-and-execution platform that helps the right blood reach the right facility at the right time while preserving clinical safety, local ownership and accountable human authority. The deterministic synthetic release spans 30 facilities and 36,899 blood units and passes 603 automated tests plus 169/169 live role and permission probes.

## Short version

Rabta-e-Hayat is a bilingual blood-supply operating network for hospitals, blood centres and provincial teams. It combines complete vein-to-vein blood-bank workflows with probabilistic demand forecasting, expiry rescue, constrained transfer planning, emergency simulation and traceable human approvals. The working synthetic MVP covers 30 facilities and 36,899 blood units, passes 603 automated tests and 169/169 live role and permission probes, and includes a governed Qwen/DashScope gateway with a deterministic no-key fallback.
