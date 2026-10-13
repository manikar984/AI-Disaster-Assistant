# Milestone 1 — repository audit, 2026-10-13

Audit of the current implementation at commit `f02fdee` (branch base) plus the
first verified fixes landed on `arena/0aaecd40-ai-disaster-assistant`.
Every statement below was checked by running the code or reading it; items that
could not be executed in this environment are marked **unverified**.

## 1. Method and environment

| Step | Command | Result |
|---|---|---|
| Backend suite | `python -m pytest -q` (Python 3.11.2, `requirements.lock`) | **43 passed**, 1 Starlette deprecation warning |
| Dependency audit (Python) | `pip-audit -r requirements.lock` | No known vulnerabilities |
| Dependency audit (npm) | `npm audit --prefix web` | 0 vulnerabilities |
| Frontend build | `npm run build --prefix web` | `tsc -b` + Vite build pass (480 kB JS, 39.7 kB CSS) |
| Pages base build | `npm run build --prefix web -- --base=/AI-Disaster-Assistant/` | `index.html` references `/AI-Disaster-Assistant/assets/…` only |
| Full-stack browser smoke | `bash .github/scripts/browser-smoke.sh` | Pass |
| Static-preview browser smoke | `npm run test:static --prefix web` | Pass |
| App boot + health | `uvicorn backend.app:app` → `/api/health` | `{"status":"ok","version":"0.1.0"}` |
| CI | GitHub Actions “Checks” run [38434775564](https://github.com/manikar984/AI-Disaster-Assistant/actions/runs/38434775564) | **All 10 steps pass** |
| Secret scan | `git grep` over every revision for bearer tokens/API keys | No match; repository history is a single commit |

Not executable here, therefore **unverified**: live USGS fetch (outbound egress
is restricted to package/GitHub hosts), real SMTP delivery, Docker/Compose build
(no Docker daemon), the rendered GitHub Pages site (`github.io` is not
reachable from this sandbox), and any load/penetration/accessibility testing.

## 2. What actually works

* **Backend service.** FastAPI 0.143 on Python 3.11 with typed Pydantic models
  (`extra='forbid'`), versioned SQL migrations applied transactionally at
  startup, SQLite in WAL mode with foreign keys enforced.
* **Authentication and private data.** Registration, login, logout, profile,
  account deletion; scrypt password hashing with per-user salts and constant
  time comparison; opaque session tokens stored only as SHA-256 digests; cookies
  `HttpOnly` + `SameSite=strict` (+`Secure` when configured); ownership checks on
  locations, notifications and reports; admin role enforced server-side.
  `/api/me`, `/api/locations`, `/api/notifications` return 401 for guests, and a
  second account cannot read or delete another account’s rows (tests cover both).
* **Ingestion pipeline.** USGS GeoJSON adapter with strict normalization,
  idempotent `UNIQUE(source, external_id)` upserts, per-record savepoints so one
  malformed record cannot poison a batch, run bookkeeping in `ingestion_runs`,
  and source health (`healthy`/`degraded`/`error`/`stale`) visible to users.
  Feed failure never substitutes sample data.
* **Classification and trust separation.** Transparent rule-based screening for
  eight hazard categories with negation/metaphor guards; official magnitude
  mapping; severity, confidence and verification are separate fields; community
  volume never upgrades verification; official event IDs are never merged.
* **Notification qualification.** Documented gates: live/delayed provenance,
  ≤6 h freshness, corroborated or officially confirmed, coordinates present,
  radius + minimum severity per saved location, `(user, incident, version)`
  uniqueness, 20 non-critical alerts/hour/user, resolved/disputed follow-ups to
  previously notified users only.
* **Delivery.** Durable outbox rows per channel; SMTP with STARTTLS, stable
  `Message-ID`, exponential backoff, cancellation when consent is withdrawn or
  the incident version is superseded; `accepted` is explicitly not claimed as
  “delivered”; in-app delivery works without external services.
* **Frontend.** Responsive dashboard, filters, search, offline-capable vector
  map with optional street tiles, evidence/timeline detail panel, account and
  saved-location flows, alert history with per-channel delivery state, source
  health page, admin review form, focus-trapped dialogs, reduced-motion support.
* **GitHub Pages build.** Project-path base applied to that workflow only; the
  application build stays at the root path (both verified above).
* **CI.** Backend tests, both dependency audits, the frontend build and both
  browser smokes pass on the runner.

## 3. Partially implemented

| Area | State | Missing |
|---|---|---|
| Data sources | One adapter (USGS) hard-coded in the worker | Adapter registry, per-source configuration, enable/disable at runtime, RSS/Atom and CAP adapters, additional official feeds, per-source counters for skipped/rejected records |
| Detection | Deterministic rules + structured magnitude | NLP/entity extraction, location resolution from free text, calibrated scoring, optional grounded LLM summarisation, independent-source corroboration |
| Incident grouping | Conservative proximity/time/type grouping | Reviewable merge/split, conflicting-claim display, affected-area/precision fields |
| Accounts | Password auth with CLI-only recovery | Email verification, self-service recovery tokens, session list/revoke-all, idle timeout, MFA |
| Notifications | In-app + SMTP outbox | Browser push, SMS, WhatsApp; per-channel provider limits; dead-letter replay UI |
| Administration | Severity/verification review + audit + JSON inspection | Incident merge/split, type correction, moderation queue, user management, delivery replay, readable ops UI |
| Saved locations | Add/remove, radius, minimum severity, email consent | Editing, place search, category filters, per-channel preference screen |
| Live updates | 15 s polling with an error banner | SSE/WebSocket with reconnect/backoff, explicit stale-data indicator, incremental updates |
| Observability | Structured worker logs, run/attempt records, `/api/health` | Worker heartbeat, `/api/ready`, retention purger, metrics/SLOs, alerts for stale feeds |
| Deployment | Dockerfile, Compose, runbook, HTTPS reverse-proxy example | Actual hosted deployment, managed database, verified backups, rollback drill |

## 4. What was broken — reproduced, fixed, verified

**CI browser smoke failure (reported by the owner).** Reproduced from CI evidence
rather than guessed: the step failed while every preceding step passed. The raw
job log and artifacts are unreachable from a restricted network
(`results-receiver.actions.githubusercontent.com`, `*.blob.core.windows.net`),
so the failure was made diagnosable first — the smoke test now prints a
checkpoint per phase and a diagnostics block, and the runner emits the test log
tail as check annotations plus an uploaded artifact.

The annotations then showed the real cause:

```
error: Search for a sample location returns exactly one row / 5 !== 1
```

The test typed “Japan” into the search box and immediately read
`.incident-row` count. That races the re-fetch, so CI measured the unfiltered
sample list (5 rows) while the slower local machine happened to read the
filtered list (1 row). Fixed by awaiting the settled row count and still
asserting the row content, so nothing was weakened. CI is green:
run [38434775564](https://github.com/manikar984/AI-Disaster-Assistant/actions/runs/38434775564).

**GitHub Pages misrepresentation.** The Pages workflow is configured
(`build_type: workflow`, `status: built`) and the project-path assets resolve,
but the hosted static build has no API: `/api/*` returns GitHub Pages’ HTML 404.
The dashboard previously surfaced a raw JSON parse error and an empty feed that
could be read as “no incidents anywhere”. The built frontend now probes
`/api/health`, and when no backend answers it states that no backend is
connected at this address, shows unknown (—) statistic values, replaces the feed
with “Backend not connected”, removes the sample-data shortcut, explains why
sign-in/report actions are unavailable, and never renders incident, sample or
account data. A new `npm run test:static --prefix web` test serves the built
frontend with no API (404 HTML for `/api/*`, exactly like Pages) and asserts all
of the above.

## 5. Integrations and the credentials they require

| Integration | Requirement | Current state |
|---|---|---|
| USGS earthquake feed | Outbound HTTPS only, no account | Adapter implemented and unit-tested with mocked HTTP; **live egress unverified here** |
| Additional official feeds (GDACS, WMO/NOAA CAP, IMD/NDMA bulletins) | Availability/redistribution checks per source | Not implemented; must be evaluated before promising coverage |
| RSS/Atom news | Feed URLs and each publisher’s usage policy | Adapter not implemented |
| SMTP email | Provider host, port, verified sender, credentials, quotas | Outbox implemented; no provider configured, so no real message has been sent |
| X/Twitter | Paid API plan plus a **rotated** bearer token | Legacy code only; not wired to the worker. The token removed from `prototype.py` must still be revoked by the owner |
| Browser push | VAPID keypair (self-generated, no purchase) | Not implemented |
| SMS / WhatsApp | Provider account (for example Twilio/Meta), sender IDs, costs | Not implemented |
| Hosting | Persistent service + worker + durable database + HTTPS | Not provisioned; needs the owner’s approval and account |
| LLM assistance (optional) | Provider key, cost/limits, data-handling review | Not implemented; the platform runs without it |

## 6. Security review

Working controls: scrypt (n=16384, r=8, p=1) with random salts; SHA-256 session
digests; `HttpOnly`/`SameSite=strict` cookies; server-side authorization on
every private route; JSON-only bodies plus same-origin checks and a required
`X-Requested-With` header on mutations; 20 000-byte body cap enforced before
routing; fixed allowlist of outbound feed URLs (no user-supplied fetch URLs, so
no SSRF surface in the adapters); React escaping for all rendered source text;
`rel=noreferrer` on external links; error responses carry no stack traces;
`.env` is ignored and secrets are never bundled into the frontend; dependency
audits are clean; no credentials exist in the working tree or history
(history is a single commit).

Gaps to close before public production use:

1. **Missing browser security headers** — no `Content-Security-Policy`,
   `X-Frame-Options`/`frame-ancestors` (clickjacking), HSTS (currently only in
   the example reverse-proxy config), or COOP/CORP.
2. **Rate limiting is in-process and proxy-blind** — `request.client.host` is
   the proxy address behind a load balancer, so all users share one bucket; a
   single abuser can exhaust the write budget for everyone, and a restart resets
   counters. Needs edge limiting and/or a shared store before real traffic.
3. **No self-service account recovery or email verification** — recovery is
   operator-assisted via CLI. Acceptable for a pilot, not for public sign-up.
4. **No CSRF token** — the JSON + origin + custom-header model is defensible and
   tested, but a per-session token would be stronger if any HTML form or
   third-party embed is ever added.
5. **Admin surface** — no incident merge/split, moderation or user management,
   and the admin overview returns raw rows. Privileged operations must record
   actor, timestamp, action and reason (review flow already does).
6. **No retention enforcement** — documented policy (raw payloads 30 days,
   delivery history 90 days, audit 1 year) is not implemented.
7. **Backups are documented, not automated or rehearsed.**
8. **Single-node database** — SQLite serialises writers and cannot be shared
   between multiple API/worker replicas; WAL files must never be copied naively.
9. **SMTP is at-least-once** — a crash after a provider accepts a message but
   before the local commit can duplicate an email.
10. **No worker liveness signal** — `/api/health` checks only the API and
    database; a dead worker is visible only as stale source timestamps.
11. **Testing gaps** — no load, penetration, accessibility or disaster-recovery
    test; no measured latency or accuracy figures, so none are claimed.

## 7. Database and scaling constraints

SQLite with WAL is sound for a single API process plus a single worker on one
host with a persistent volume: it is simple, transactional, constraint-enforced
and backup-able (`Connection.backup()`). It does not support multiple writers or
horizontal scale-out, offers no spatial indexing, and has no server-side
authentication or row-level security. PostGIS-grade radius/within queries are
currently served by Python-side Haversine over a bounded candidate set.

Recommended path: keep SQLite for the pilot, and port to PostgreSQL + PostGIS
before multiple replicas, before spatial “within area” queries, and before any
deployment that requires point-in-time recovery. Schema, migrations and the
access layer are already parameterised, so the port is mechanical but must be
rehearsed with a real data copy.

## 8. Required changes before public production use

1. Provision a persistent host with HTTPS, API + worker + durable volume, and
   verify the full checklist in section 19 of the project brief.
2. Move rate limiting to the edge/shared store; add CSP/frame-ancestors/HSTS.
3. Implement account recovery + email verification, session revocation and
   idle expiry; add email verification before notifications are enabled.
4. Add retention enforcement, automated encrypted backups and a restore drill.
5. Add worker heartbeat / readiness and stale-feed alerting.
6. Reach at least two independent, non-USGS evidence sources with documented
   provenance before claiming “cross-checked” incidents.
7. Verify one notification channel end-to-end with a consenting recipient and
   record the result.
8. Publish a privacy notice, moderation procedure and incident-response contact.
9. Rotate the previously exposed X token and keep it out of the repository.
10. Re-run the security, dependency and browser suites against the deployed
    commit, and record actual measured latency instead of targets.

## 9. Prioritised implementation plan

**Delivery status: P0 items 1, 2 and 3 are implemented and verified on
`arena/0aaecd40-ai-disaster-assistant` (source registry with USGS/GDACS/RSS/CAP
adapters, per-source health, worker heartbeat, `/api/ready`, `/api/policy`);
see `docs/STATUS.md` for the live board and `docs/VERIFICATION.md` for the
commands behind every claim. Items 4-8 and P1/P2 remain open.**

**P0 — trust, correctness and security (no paid credentials needed)**

1. Source-adapter registry with per-source configuration, health, counters and
   idempotent processing; add RSS/Atom and CAP adapters plus a second official
   feed, with end-to-end tests over real HTTP against a local fixture server.
2. Ingestion observability: worker heartbeat, `/api/ready`, per-source
   freshness/staleness surfaced in the UI and admin views.
3. Account security: email verification + expiring single-use recovery tokens
   (SMTP-gated, testable with a local fake SMTP server), session list/revoke-all,
   idle expiry, and hardened rate limiting.
4. Security headers (CSP, frame-ancestors, HSTS via proxy) and a documented
   threat-model update.
5. Retention/backup/restore tooling with a test that restores a database.
6. Live updates via SSE with reconnect/backoff and polling fallback, plus
   explicit stale-data indicators.
7. Administration: reviewable incident merge/split, type correction, moderation
   queue, user management and delivery replay, all audited.
8. Frontend: incident detail routes, saved-location editing and place search,
   notification preference management, map clustering, loading/partial-failure
   states, and accessibility/KB-navigation tests.

**P1 — needs owner accounts, credentials or approval**

9. Deployment to a persistent host (approval required; cost estimate to be
   confirmed before creating anything) with Postgres/PostGIS migration and
   automated backups.
10. SMTP provider configuration and a verified end-to-end email test; then
    Browser Push (VAPID) and optionally SMS/WhatsApp adapters.
11. X/social adapter behind an approved paid plan, with attribution and
    deletion-policy compliance.

**P2 — later**

12. Trained/evaluated NLP with calibrated (never implied) confidence, entity
    extraction, affected-area modelling, multi-worker scaling with queue leases,
    metrics/SLO dashboards, load/penetration/accessibility testing and a
    disaster-recovery rehearsal.

## 10. Legacy prototype (unchanged finding)

`prototype.py`, `twitter_api.py`, `disaster_data.json` and `disaster_map.html`
remain as historical artefacts. The Streamlit prototype fetched X posts with a
placeholder credential, wrote into its own fixture, mapped unknown locations to
India and interpolated popup HTML. The apparent token is absent from the working
tree, and repository history is now a single commit with no credential in it —
but earlier public history, forks, clones and provider caches are not covered by
that, so **revocation and rotation by the owner remain outstanding**. `disaster_data.json` (42 records, 39 without usable
locations, no reliable timestamps) is now reachable only through the explicit
`python -m backend.cli sample` import and is labelled as historical sample data
everywhere it appears.
