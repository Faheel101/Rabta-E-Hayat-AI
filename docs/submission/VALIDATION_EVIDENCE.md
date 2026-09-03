# Submission candidate validation — 3 September 2026 PKT

This report records the last pre-publication checks performed against a clean copy containing only files Git would publish.

## Passed

- Publishable source package: **4.5 MB** before generated runtime state.
- Secret hygiene: `.env`, every `.env.*` variant except `.env.example`, databases, WAL files, backups, virtual environments, caches, artifacts and `node_modules` are excluded.
- Candidate file-size check: no publishable file exceeds 1 MB; the largest is the presentation at approximately 388 KB.
- Front-end reproducibility: `npm ci --ignore-scripts` and `npm run css` passed; generated CSS exactly matched the submitted asset.
- Deterministic data build: `python -m scripts.rebuild` passed from source in 459.8 seconds.
- Generated release state: 10 organizations, 30 facilities, 15 active users, 7,460 synthetic donors, 36,899 blood units, 43,200 forecast rows and 189 constrained transfer recommendations.
- Automated suite: **603 passed, 5 skipped** in 128.66 seconds.
- Release gate: configuration, schema, assets, integrity and demo-data checks all passed.
- Live smoke gate: **169/169** route, role and permission probes passed across eight identities representing seven roles; slowest response was 236.3 ms on the test machine.
- Presentation: all 10 slides were inspected individually; the final PowerPoint overflow test passed.
- Compact judge dataset: the 117,266,637-byte ZIP passed archive integrity, release readiness and 169/169 live route/role probes after retaining more than one year of history.

## Intentionally pending

- A clean Docker Compose launch could not be performed on the preparation machine because Docker is not installed. The Dockerfile and Compose contract are covered by release tests, but the final container launch should still be run before submission.
- The Qwen/DashScope gateway is implemented, but no external API key is configured. The release therefore demonstrates the explicitly labelled deterministic fallback.
- GitHub Actions and public-link checks require the first public push.

This is a realistic synthetic demonstration MVP, not certified clinical software and not evidence of production deployment readiness.
