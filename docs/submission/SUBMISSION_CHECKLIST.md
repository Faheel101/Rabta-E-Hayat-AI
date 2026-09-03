# Submission checklist — closes 4 September 2026, 23:59 PKT

## Portal access

- [ ] Open the password email from `no-reply@aihackathon.cognix-pk.com`.
- [ ] Set a unique portal password; do not store it in this repository or share it in chat.
- [ ] Record every required field, accepted file type, size limit, link requirement and pitch-duration/slide limit.
- [ ] Confirm whether the portal requires a deployed URL, video link, repository link, presentation upload, team details or Alibaba Cloud service identifiers.

## Public GitHub repository

- [ ] Confirm the owner account/organization and repository name.
- [ ] Decide whether to add an open-source license; public visibility alone does not grant reuse rights.
- [x] Prepare the reviewed source package without `.env`, databases, backups, local environments, caches or `node_modules`.
- [x] Run filename, file-size and content secret checks against the publishable candidate.
- [ ] Create the initial commit on `main`, add the GitHub remote, and push.
- [ ] Confirm GitHub Actions passes on the public commit.
- [ ] Open the repository in a private/incognito browser and verify README links and Mermaid diagrams render.

## Reproducibility and evidence

- [ ] Clone the public repository into a new empty directory.
- [ ] Copy `.env.example` to `.env` and leave API-key fields blank.
- [ ] Run `docker compose up --build -d`.
- [ ] Confirm `/health/live` and `/health/ready` return HTTP 200.
- [ ] Run `python -m scripts.release_check` and the live `demo_smoke` probes.
- [ ] Confirm the demo login page, English/Urdu switch, guided showcase and Expiry Rescue decision flow.

## Presentation and demonstration

- [ ] Add the confirmed team identity and repository URL to the final deck.
- [ ] Keep claims consistent with the actual API state: Qwen connected, deterministic fallback, or both demonstrated.
- [x] Export and inspect the final `.pptx`; export PDF only if the portal accepts or requires it.
- [ ] Rehearse the four-minute flow twice from a fresh browser session.
- [ ] Prepare one local backup, one cloud copy and screenshots/video for failure recovery.

## Final portal submission

- [ ] Paste the reviewed copy from `PORTAL_COPY.md`.
- [ ] Upload/link the final presentation and repository.
- [ ] Open every submitted link from an incognito browser.
- [ ] Save a screenshot/PDF of the completed form before final submission.
- [ ] Submit well before 23:59 PKT and save the confirmation/reference number.
