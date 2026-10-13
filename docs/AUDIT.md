# Milestone 1 — repository audit

Inspected all six tracked files, including presentation slide XML and generated map HTML. Baseline: cd09353. No tests or dependency manifest existed; README's installation command was broken. Legacy application was inspected statically, not certified as runnable.

* `prototype.py`: Streamlit table, location filter, keyword classifier, severity heuristics, seven-word excerpt, manual text analysis, Folium view. Repeated fetch/merge blocks; mutations to the fixture; unknown coordinates incorrectly mapped to India; custom input lost on rerun. No ML or OpenAI implementation despite footer claim.
* `twitter_api.py`: Tweepy recent search and deduplication helper, but placeholder credential, failures returned as empty frames, no durable scheduling/backoff, no original URLs, and sample/live provenance lost on write.
* `disaster_data.json`: 42 records, 41 unique texts, 39 unknown locations. Includes metaphorical/non-disaster posts. No usable publication timestamps or evidence URLs. Preserve as historical sample fixture, never treat as live evidence.
* `disaster_map.html`: generated Leaflet artifact with external CDN dependencies; not a service.
* Presentation: describes the keyword-based prototype; no additional implementation.
* README: feature descriptions overstate reliability, missing requirements file, placeholder clone URL.

## Security and readiness
Apparent bearer credential in prototype removed from working tree. **Owner must revoke it and review Git history exposure.** History is not rewritten here. No authentication, authorization, validation boundary, audit log, migrations, health checks, deployment definition, or CI existed. Popup HTML interpolation risk in legacy app; not used by new application.

## Preserve / evolve
Keep legacy files and original data for reproducibility. Stop fixture mutation, remove secret, do not plot unknown coordinates, remove unsupported OpenAI attribution. New backend retains the transparent heuristic baseline but never grants verification from keyword or volume alone. New frontend replaces Streamlit as the main entry point.

## Ordered plan
2. Document contracts, schemas, trust rules and single-node deployment.
3. Add persistent normalized reports, explicit sample mode, official USGS adapter, polling worker, conservative classification and incident grouping.
4. React responsive feed/map/details, polling updates, account and saved-location controls.
5. Geographic matching, qualified in-app alerts, durable email outbox with retries and suppression.
6. Admin status review and immutable audit entries, source health and delivery inspection.
7. Execute backend/security tests and frontend typecheck/build; document unverified workflows.
8. Provide container and deployment/runbook. Public production deployment requires operator infrastructure and credentials; preview is not production certification.
