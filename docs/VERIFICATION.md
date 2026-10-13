# Verification record — 2026-10-13

## Executed successfully

| Check | Actual result |
|---|---|
| `python -m pytest -q` | **43 passed**. One upstream TestClient deprecation warning recommending httpx2; no failing tests. |
| `npm run build --prefix web` | TypeScript check + Vite production build passed. |
| `npm run test:browser --prefix web` | Headless Chromium desktop/mobile integration smoke passed against a separate, disposable database. |
| `pip-audit -r requirements.lock` | No known vulnerabilities reported for the resolved Python dependency set. |
| `npm audit --prefix web` | 0 vulnerabilities reported. |
| `git diff --check` | Passed after removing legacy trailing whitespace. |
| Python compile check | Backend and both legacy Python files compile successfully. Legacy runtime dependencies not exercised. |
| Preview HTTP smoke | `/api/health` returns `{"status":"ok","version":"0.1.0"}`; browser loaded built frontend and API. |

Browser smoke exercised feed, sample selection, map render, incident evidence, search, registration, account session, save/remove location, community report submission, account deletion, 390px mobile navigation and no horizontal overflow on the mobile sources page. Captured desktop/mobile dashboard rendering for local inspection. No browser JavaScript exceptions during the smoke workflow. Initial Chromium launch lacked NSS system libraries; the test now extracts the runtime libraries included in the npm browser package. Browser web-security disabling flags are removed from the launch arguments.

Backend coverage includes ownership of locations/notifications, unauthenticated/admin access denial, CSRF origin/header checks, registration/logout/login, password salt verification, input/body validation, duplicate suppression, sample isolation, geographic matching, freshness/severity/trust gates, official revision handling, community grouping without trust escalation, nearby aftershock separation, failed and malformed source handling, audit/review/resolution, consent withdrawal, retry and mocked SMTP acceptance.

## Not verified / not claimed

* Real USGS feed access is blocked by this environment's outbound-host restriction. Adapter tests mock HTTP responses; these are not evidence of successful live fetching.
* Real SMTP delivery is not configured. Tests exercise a fake SMTP server interface and genuine missing-configuration retry behavior; no actual email was delivered.
* Arena exposes a preview for the running service on port 8000. This is **not a durable production deployment**. The externally proxied HTTPS URL, domain configuration and production secure-cookie flow were not independently verified.
* Docker/Compose build and startup were not executed. Configuration is provided for operator validation.
* CI workflow is written but has not run on GitHub; local tests passing do not imply GitHub checks passed.
* No load test, penetration test, disaster recovery rehearsal, formal accessibility audit, statistical ML evaluation, push/SMS/WhatsApp testing or measured production latency/SLO.
* Sample data has no reliable original publication times. Fixture timestamps and approximate region coordinates are disclosed; samples are never used as live evidence.

## Milestone status

1. **Audit complete:** all six original files examined, apparent exposed credential identified/removed, working concepts preserved.
2. **Pilot architecture complete:** data model, contracts, trust/security boundary, deployment approach documented. PostgreSQL/PostGIS scale-out deferred.
3. **Core backend implemented for pilot scope:** USGS adapter, static import, community reports, persistence, heuristic/structured screening and grouping. Broader sources, trained NLP, entity extraction and automatic corroboration are not complete.
4. **Website implemented/tested for pilot scope:** responsive dashboard/map/details, polling, account and preferences. Recovery is operator-assisted; clustering and affected-area overlays deferred.
5. **Notifications partial:** in-app fully exercised; SMTP outbox tested with mocks, not real provider; push/SMS/WhatsApp absent.
6. **Administration partial:** severity/status review, audit, source/delivery inspection. Merge, type correction and full moderation/escalation workflows absent.
7. **Pilot checks passed:** listed tests/build/scans. Production security/reliability certification has not occurred.
8. **Deployment guide/configuration delivered; local preview running. Public production deployment not completed.**
