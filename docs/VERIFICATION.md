# Verification record

## 2026-10-13 — deployment preparation (nothing deployed)

| Check | Actual result |
|---|---|
| Repository + documentation review | `docs/DEPLOYMENT.md`, `docs/HOSTING.md`, `docs/OPERATIONS.md`, `docs/STATUS.md`, `docs/VERIFICATION.md`, `deploy/*`, `.env.example`, `Dockerfile`, `compose.yaml` read end to end |
| Defect found | The runbook installed the application in `~/AI-Disaster-Assistant` while the systemd units run from `/opt/disaster-assistant` **with `ProtectHome=true`** — a literal first deployment would have failed with the services unable to see the code. Corrected in `docs/DEPLOYMENT.md`, required by `docs/DEPLOY-CHECKLIST.md`, and pinned by a new test |
| Defect found | `docs/HOSTING.md` described the Oracle host as running `docker compose`; the verified route is the systemd units + Caddy in `deploy/` (the compose stack has no TLS proxy and no test coverage). The document now says so |
| Defect found | `docs/OPERATIONS.md` still framed `docker compose` as the deployment and called the secret file `0600`; the authoritative route (systemd + Caddy, the path the tests cover) is now stated, and the mode corrected to the `0640 root:disaster` that `provision.sh` really creates — a root-only file would start the services but break every manual proof command in the runbook |
| Environment-file mechanism, verified locally | `set -a; . /etc/disaster-assistant/env; set +a` really does export the values to the child process (checked with a stand-in file and the project's own CLI). A `0640` file owned by another account is unreadable by an unrelated user, and readable once the reader is in the file's group; `sudo -u <account-with-nologin-shell> bash -c …` runs the command directly, which is the pattern used throughout the checklist |
| Documentation consistency | 8 tests assert that the runbooks and the scripts agree on `/opt/disaster-assistant`, that the checklist keeps its five staged gates and its two honesty rules ("never manufacture an event", "verify on the VM"), that every path, service name and script it names exists, and that the fact collector hands the operator a secret-free evidence sheet |
| `pytest -q` | **133 passed** |
| `deploy/collect-facts.sh` (read-only, no secrets) | Ran locally against a throwaway environment file: exit 0, degrades cleanly on a host with no systemd/caddy/sqlite3, reports `SMTP_PASSWORD=<set>` but never the value — including for a key it has never seen. Two defects found by running it here were fixed: a GNU-awk-only regex that matched nothing under Ubuntu's mawk, and a catch-all that would have printed an unknown key's value. 5 tests pin both |
| Hard limit of this work | **No VM, account, DNS record, certificate, feed fetch or email has been created or executed.** Oracle-specific behaviour (arm64 images, A1 capacity, Security List, the image's iptables ruleset, ACME issuance, the nightly timer) remains unverified and is listed as such in `docs/DEPLOY-CHECKLIST.md` §13 |

## 2026-10-13 — full sweep after the enumeration, restore-drill and header work

Every command below was run on the final commit of this set (`7458b82`), in this
sandbox, against the code as pushed.

| Check | Actual result |
|---|---|
| `python -m pytest -q` | **125 passed**, 1 third-party deprecation warning (Starlette/httpx `TestClient`) |
| `pip-audit -r requirements.lock` | No known vulnerabilities found |
| `npm audit --prefix web` | 0 vulnerabilities |
| `npm run build --prefix web` | Passed: `index-et3bNit-.js` 487.88 kB (151.75 kB gzip), `index-dz-O9zzX.css` 40.90 kB (12.73 kB gzip) |
| `PYTHON=.venv/bin/python bash .github/scripts/browser-smoke.sh` | Passed: browser suite under the enforced CSP with **zero violations**, then the release gate against the same live server — **12 passed, 0 failed** |
| `npm run test:static --prefix web` | Passed |
| `bash deploy/verify-live.sh http://127.0.0.1:8000` (live pilot, API + worker) | **12 passed, 0 failed, exit 0** — health, database, worker heartbeat (age 1 s), policy, feeds, sample provenance, frontend, and the five header checks |
| `bash deploy/verify-live.sh http://127.0.0.1:1` (nothing listening) | 13 failures, `Release gate NOT met`, exit 13 |
| Live pilot registration probe | new address and existing address both `202` with the identical body; `login` `200`; `/` carries the CSP, `X-Frame-Options: DENY` and `nosniff` |
| CI (GitHub Actions, push **and** pull_request) | Green on `b2bc5a0`, `2c5f3ef` and `7458b82` — runs `38445483717`/`38445493780`, `38446546887`/`38446554845`, `38446713777`/`38446723900` |

No regressions were found by the sweep, and no test was weakened to reach it: the
count went 99 → 105 → 125 across these three tasks, with the changed tests
(registration now signing in explicitly, the release gate's failure count) made
stricter rather than looser.

## 2026-10-13 — backup/restore drill and browser security headers

| Check | Actual result |
|---|---|
| `pytest tests/test_restore.py -q` | 10 passed |
| `pytest tests/test_headers.py -q` | 10 passed |
| `pytest -q` | **125 passed**, 1 third-party deprecation warning |
| Drill (isolated data, real scripts) | seeded database → `deploy/backup.sh` → `deploy/restore.sh <snapshot> --to <fresh path>`: snapshot and restored copy both report `integrity ok`, no broken references, all 15 required tables, and row counts identical across users, sessions, locations, sources, reports, incidents, incident_reports, timeline, notifications, deliveries, audit and tokens |
| Restored database is *usable* | the API runs against the restored file: sign-in with the password from the snapshot (`200`), `/api/incidents` returns the seeded incident with its timeline and evidence, the queued notification and saved location are there, and a **new** location posts successfully |
| Restored database accepts new work | `ingest()` + `qualify_notifications()` against the restored file produce a new notification for a new qualifying incident, and `migrate()` is a no-op (the snapshot is migration-complete) |
| Relationships preserved | every `locations`/`sessions` row resolves to a user, every `incident_reports` link resolves both ways (the seeded pair included), every `notifications` row resolves to an incident and a user, every `deliveries` row resolves to a notification, per-user rows stayed separate, and `runtime_state` (worker heartbeat) survived |
| Live database untouched | SHA-256, size, mode and mtime of the source database identical before and after the drill; no rollback journal left behind |
| Refusals (the safety rails) | no `--to` → exit 2 "Refusing to guess a destination"; existing target without `--force` → exit 1 and the target is byte-identical afterwards; `--to` pointing at `DATABASE_PATH` without `--force` → exit 1 "live database"; a file that is not a database → "not a usable SQLite database"; a snapshot with a dangling foreign key → "broken foreign-key reference"; in every failing case **no target file and no temporary file were left behind** |
| Security headers on every response | `/`, `/api/health`, `/api/policy`, a missing asset, a 404, `401`, `403` (bad origin *and* missing header), `413` (oversized body) and `429` (rate limited) all carry all six headers — including the responses the boundary middleware returns before any route runs, which is what pins the middleware order |
| Policy content | `default-src`/`script-src`/`connect-src` = `'self'` with no `'unsafe-inline'`/`'unsafe-eval'`; `frame-ancestors`, `object-src`, `base-uri` locked down; `style-src 'self'` with the single `style-src-attr 'unsafe-inline'` exception; `img-src` allows exactly the tile host read out of the frontend source plus `data:` for Leaflet's sprites; `Permissions-Policy` denies geolocation/camera/microphone/payment/USB, and the frontend contains no `navigator.geolocation` |
| Browser smoke under the policy | full journey passes with **zero `securitypolicyviolation` events** (the runner now fails on any violation) — the map, Leaflet's inline positioning, the bundle and the stylesheet all load under `script-src 'self'` |
| Static preview smoke | passed (GitHub Pages serves its own headers; the app's policy is served by the app) |
| Proxy parity | `deploy/Caddyfile` carries the same CSP string as `backend/security_headers.py`, asserted byte for byte by a test, plus HSTS which stays at the proxy |

Not covered here: the off-host encrypted copy of the backups, a drill from a real
snapshot on the Oracle host, and a `report-uri` collector for CSP violations.
Unhandled-exception 500s (Starlette's outermost error middleware) carry the
proxy's headers but not the application's — documented in
`backend/security_headers.py`.

## 2026-10-13 — registration enumeration: removed, and measured

| Check | Actual result |
|---|---|
| `pytest -q` | **105 passed**, 1 third-party deprecation warning |
| `pytest tests/test_auth.py -q` | 18 passed (6 new regression tests) |
| Live probe against a running server (new address vs existing address) | identical `202` and identical body: `{"ok": true, "message": "If that address can receive mail, a message is on its way to it. Sign in with your password to continue."}`; identical header set; **no `Set-Cookie`** either way |
| Response time, before the fix | new address median 50–68 ms, **existing address 8.9 ms** — the duplicate path skipped the password hash, so existence was testable through latency alone |
| Response time, after the fix (8 probes each, 10/min limit respected) | never registered: median **50.2 ms** (48.3–54.3); already exists: median **52.6 ms** (49.1–63.9); **ratio 1.05** |
| Behaviour on a duplicate sign-up | the existing account is untouched (password hash and `email_verified` unchanged, still one user row), the caller gets no session (`/api/me` and `/api/sessions` are `401`), an audit row `register-duplicate` is written, and the address owner receives the notice instead |
| Mailbox-bombing guard | 5 duplicate sign-ups produced 1 notice and 4 suppressed attempts (`notice already sent inside the cooldown`); registration plus first duplicate = 2 messages total |
| Ordering (the timing defence, asserted in a test) | the response body completes **before** the SMTP conversation starts: `events == ['response-complete', 'smtp-started']` — `mailer.send` was wrapped, and still called for real |
| Frontend | registration returns to the sign-in form (no session), and the browser smoke test signs in with the password it just registered — passed |
| Browser smoke (full stack) | passed, including the new register→sign-in checkpoint |
| Static preview smoke | passed |

Not covered here: whether a provider's spam filter treats the notice email like
any other transactional message (that needs the SMTP credentials on the host),
and there is no CAPTCHA — the endpoint is rate-limited per client (10/min) and
the notice has its own per-address cooldown.

## 2026-10-13 — retention enforcement

| Check | Actual result |
|---|---|
| `pytest tests/test_retention.py -q` | 5 passed |
| `pytest -q` | **99 passed**, 1 third-party deprecation warning |
| `python -m backend.cli retention --dry-run` (pilot database) | `Would remove: nothing.` — every class inside its window, nothing deleted |
| Dry run against seeded data | Reports the seven expired classes with exact counts, leaves the raw payload, both reports, all four incidents, both notifications and both audit rows untouched |
| Real purge against seeded data | Removed exactly: 1 raw payload (nulled, the report row kept), 1 ingestion run, 1 delivery + 1 notification, 1 inactive unverified incident, 1 expired session, 1 expired token, 1 old audit row. Kept: the corroborated incident, the fresh incident, the sample incident, the fresh notification/delivery, the live session/token, the fresh audit row, and the account row |
| Idempotence | Second purge returns `{}` — nothing is reported as deleted twice |
| Worker scheduling | `purge_if_due()` purges and writes a `runtime_state` marker; an immediate second call returns `None`; after ageing the marker past the window it runs again |
| Live pilot (API + worker on port 8000) | The running worker applied the policy on its first cycle and wrote `runtime_state.last_purge` = `{"purged": {}, "policy_days": {...}, "at": "2026-10-13T17:57:24+00:00"}` — nothing was due, so nothing was deleted |

Not covered here: the off-host backup copy, the monthly restore drill, and any
legal review of the audit window — those stay owner/operator tasks.

## 2026-10-13 — email channel end-to-end and the release gate

| Check | Actual result |
|---|---|
| `pytest -q` | **94 passed**, 1 third-party deprecation warning (Starlette/httpx in `TestClient`) |
| `pytest tests/test_email.py -q` | 13 passed — every transport mode over a real loopback socket |
| Implicit TLS (`SMTP_SECURITY=ssl`, port 465 semantics) | PASS: TLS wraps the socket before the greeting, `EHLO` first, **no** `STARTTLS` attempted, `AUTH PLAIN` accepted, message stored, delivery `accepted`, `attempts=1`, `sent_at` set |
| Implicit TLS certificate verification | PASS (fails closed): without the fixture CA the send is refused, no message stored, delivery `retry` with `CertificateVerifyError`-style text — there is no switch to disable verification |
| Cancellation gates | PASS: stale alert (published 7 h ago) → `cancelled`, **0 attempts**, no socket traffic; superseded version → `cancelled`, 0 attempts; withdrawn consent → `cancelled`; a `Resolved` correction 3 days old is **not** suppressed and is delivered |
| `deploy/verify-live.sh http://127.0.0.1:1` (nothing listening) | 8 failures, `Release gate NOT met`, exit status 8 — the gate cannot pass vacuously |
| `deploy/verify-live.sh http://127.0.0.1:8000` (live pilot) | **7 passed, 0 failed**, exit 0: health, database, worker heartbeat live (age 0 s), evidence policy, live feed, sample provenance, frontend served |
| `PYTHON=.venv/bin/python bash .github/scripts/browser-smoke.sh` | PASS — the runner now starts **API and worker together**, waits for `/api/ready` `worker: alive`, runs the 11-checkpoint browser suite, then executes the release gate against the same live server: `checks passed: 7, failed: 0` |
| `GET /api/ready` now reports the mail channel | `checks.email = configured` or `not configured` (peer of `database`/`worker`); verified on the pilot as `(not reported)` before the restart and `not configured` after — an unconfigured provider never blocks readiness, it is simply visible |

What this does **not** prove: that a provider relay accepts and a human inbox
receives a message. That needs the owner's SMTP credentials on the host, and the
command that closes it is `python -m backend.cli email-check --to <mailbox>`.

### Live pilot checks (running API + worker on port 8000)

| Check | Observed response |
|---|---|
| `POST /api/auth/register` (no SMTP configured) | `{"ok":true,"email_verified":false,"verification_email":"unavailable"}` — the account is created and the absent provider is reported, not claimed |
| `GET /api/me` | `{"id":2,"email":"preview-check@example.org","role":"user","email_verified":0}` |
| `GET /api/sessions` | one session, 12-character prefix, `current: true`, created/last-seen set, expiry 30 days out (no token value) |
| `POST /api/auth/forgot` for an unknown address | `202` with the same generic body a known address gets |
| `GET /api/ready` | `degraded` — `"no live source is enabled"`, `worker: alive`, heartbeat age 2 s, 4 sources `disabled` |
| `GET /api/incidents?mode=all` | 5 sample incidents; the two with coordinates report `point`, the three without report `unknown` (migration 003 applied on start) |

## 2026-10-13 — account security: confirmation, recovery, session control

| Check | Command | Actual result |
|---|---|---|
| Backend suite | `python -m pytest -q` | **86 passed** (was 74), 1 Starlette deprecation warning |
| Account suite | `python -m pytest tests/test_auth.py -q` | **12 passed**, every link redeemed through the real SMTP fixture |
| Frontend build | `npm run build --prefix web` | Passed (`index-DWvUO25B.js` 487.66 kB / `index-dz-O9zzX.css` 40.90 kB) |
| Full-stack browser smoke | `bash .github/scripts/browser-smoke.sh` | Passed, incl. the new "registration shows the confirmation banner and the resend control" and "password recovery entry point answers identically for an unknown address" checkpoints |
| Static-preview smoke | `npm run test:static --prefix web` | Passed |

Observed behaviour, all with no mocks in the delivery path:

* registering emails a 24-hour single-use confirmation link and reports
  `verification_email: sent|unavailable` — never a delivery it cannot prove;
* an unconfirmed account receives `in-app` deliveries only; after confirming, the
  next qualifying incident also queues `email`;
* confirmation tokens are single use, expire, and re-issuing one invalidates the
  previous link; `resend-verification` and `forgot` return byte-identical bodies
  for known and unknown addresses, and only the real account receives mail;
* a recovery link sets the new password, signs out every device (the old cookie
  gets 401, the old password is refused, the new one works) and cannot be replayed;
* `GET /api/sessions` lists sessions with a 12-character prefix, creation,
  last-seen, expiry and a `current` flag — never a token value;
* `POST /api/auth/logout-all` revokes all sessions; idle expiry (10 days without
  use) and absolute expiry both return 401 **and delete the row** (a defect where
  the delete was rolled back by the raised exception was found here and fixed);
* a database with only migration 001 applied upgrades cleanly: existing accounts
  are grandfathered as confirmed with an audit row, and their email alerts keep
  working (test `test_existing_accounts_are_grandfathered_as_confirmed`).

**Not verified:** the flows against a real mailbox (same provider limitation as
above), and no brute-force or account-takeover test beyond the in-process rate
limit has been run.

## 2026-10-13 — email channel: proven as far as this environment allows

| Check | Command | Actual result |
|---|---|---|
| Backend suite | `python -m pytest -q` | **66 passed** (was 58), 1 Starlette deprecation warning |
| Email suite | `python -m pytest tests/test_email.py -q` | **8 passed** |
| CLI without configuration | `python -m backend.cli email-check --to nobody@example.org` | Exit 1, `Email is disabled: set SMTP_HOST and SMTP_FROM`, no secret printed |

`tests/test_email.py` runs the whole path over real loopback TCP with a real
SMTP server and no mocks in the delivery code. Observed behaviour: the queued
delivery becomes `accepted` with `sent_at` set and one attempt; the received
MIME message carries the incident, its verification state and the "not a
government warning" label; the default `starttls` path performs EHLO → STARTTLS
→ AUTH PLAIN → MAIL/RCPT/DATA and the client **verifies** the server certificate
(the fixture's self-signed CA is trusted explicitly; there is no switch that
disables verification); rejected credentials store
`SMTPAuthenticationError…` as a `retry` with the password redacted; an outage at
a closed port escalates `retry,retry,retry,retry,failed` with 60 s → 120 s →
240 s → 480 s backoff; `email-check` prints the configuration summary
(`auth=yes`, no username, no password) and, against the fixture, reports the
provider's reply.

**Not proven, and not claimed:** delivery to a human inbox through a commercial
provider. That needs the owner's own credentials, and outbound SMTP is blocked
in this sandbox anyway. The exact procedure and the evidence to record are in
`docs/OPERATIONS.md`; until the owner completes step 3–4 there, the email
channel is `configured but not proven`.

## 2026-10-13 — Milestone 4: real ingestion breadth

Environment unchanged (Python 3.11.2, no Docker, egress limited to package and
GitHub hosts — so no external feed can be fetched from here).

| Check | Command | Actual result |
|---|---|---|
| Backend suite | `python -m pytest -q` | **58 passed** (was 43), 1 Starlette deprecation warning |
| Source adapter suite | `python -m pytest tests/test_sources.py -q` | 13 passed against a local HTTP fixture server |
| Frontend build | `npm run build --prefix web` | Passed (`index-CZULmvXn.js` 482.87 kB / `index-DmpLt5Or.css` 40.20 kB) |
| Full-stack browser smoke | `bash .github/scripts/browser-smoke.sh` | Passed (desktop feed, map, filters, evidence, auth, saved location, report, deletion, mobile nav, no overflow, no page errors) |
| Static-preview browser smoke | `npm run test:static --prefix web` | Passed (discloses the missing backend, shows no incidents, blocks misleading actions) |
| CLI source registry | `python -m backend.cli sources` | 4 adapters listed (`cap`, `gdacs`, `news-rss`, `usgs`), all `disabled`, each with its enable hints |
| CLI sample loader | `python -m backend.cli sample` | 5 historical incidents loaded, `mode=sample`, no notifications created |
| CLI poll with nothing enabled | `python -m backend.cli poll` | Exit status 1 with guidance, no data invented |
| Live pilot endpoints | `curl /api/health /api/ready /api/sources /api/policy` | `ready` reports `degraded` — `no live source is enabled`; `worker: alive`, heartbeat age 0 s; 4 sources all `disabled`; policy exposes categories (incl. Volcano), severity mapping, corroboration rule and notification gates |
| CI “Checks” | runs [38437227181](https://github.com/manikar984/AI-Disaster-Assistant/actions/runs/38437227181), [38437236199](https://github.com/manikar984/AI-Disaster-Assistant/actions/runs/38437236199) | All steps pass |

What the new adapter tests actually exercise (all over real HTTP against a local
fixture server, never mocked functions): USGS 3 records fetched → 2 inserted /
1 rejected with `degraded` health; idempotent repoll; magnitude revision → new
version + Critical; GDACS red cyclone → Storm/Critical/`Officially Confirmed`
while a non-official URL in the feed is dropped and the record rejected; three
news feeds with one syndicated duplicate → 2 inserted / 1 skipped, never
official, no coordinates claimed; CAP `Severe`+`Likely` → `Officially Confirmed`
and `Moderate`+`Possible` → `Developing`, both stored with
`geo_precision='alert-area-centre'`; malformed feed → `status='error'` with zero
incidents and no fallback data.

### Defect found and fixed while verifying the live pilot

The pilot database (created before `geo_precision` existed) reported
`geo_precision='point'` for sample rows with **no coordinates at all**, because
`ALTER TABLE … DEFAULT 'point'` backfilled them. Two fixes, both tested:

* `backend/migrations/003_geo_precision_backfill.sql` corrects stored rows
  (`point` → `unknown` where `lat`/`lon` are null);
* `app.py` applies the same guard when serving incidents and their reports, so a
  coordinate-less record can never be presented as a surveyed position.
* Tests: `test_legacy_rows_without_coordinates_are_corrected_on_upgrade`
  (upgrades a database that has only migration 001 applied, the real legacy
  shape) and `test_incident_api_never_claims_precision_without_coordinates`.

### Still unverified after this milestone

* Live fetch from USGS, GDACS, a news feed or a CAP feed: outbound access to
  those hosts is blocked here, so the parsers are verified against fixtures only.
  Each source must be enabled and polled once on the deployed host.
* Real SMTP delivery: no credentials and no egress; see the deployment plan.
* Docker Compose build (no Docker daemon) and any public HTTPS deployment
  (no cloud account is created without the owner's approval).

## 2026-10-13 — audit fixes on `arena/0aaecd40-ai-disaster-assistant`

Environment: Python 3.11.2, Node 22.22.3, npm 10.9.8, no Docker, outbound
network restricted to package and GitHub hosts (so no live feed, SMTP or
`github.io` fetch).

| Check | Command | Actual result |
|---|---|---|
| Backend suite | `python -m pytest -q` | **43 passed**, 1 Starlette/httpx deprecation warning |
| Python dependency audit | `pip-audit -r requirements.lock` | No known vulnerabilities |
| npm audit | `npm audit --prefix web` | 0 vulnerabilities |
| Frontend build | `npm run build --prefix web` | Passed (`tsc -b` + Vite; 480.39 kB JS / 39.69 kB CSS) |
| Pages base build | `npm run build --prefix web -- --base=/AI-Disaster-Assistant/` | `index.html` references `/AI-Disaster-Assistant/assets/…`, no root-relative asset URLs |
| Full-stack browser smoke | `bash .github/scripts/browser-smoke.sh` | Passed (desktop feed, map, filters, evidence, auth, saved location, report, deletion, mobile nav, no overflow, no page errors) |
| Static-preview browser smoke | `npm run test:static --prefix web` | Passed (discloses the missing backend, shows no incidents, blocks misleading actions) |
| CI “Checks” | run [38434775564](https://github.com/manikar984/AI-Disaster-Assistant/actions/runs/38434775564) | **All 10 steps pass**, including both browser smokes |

### CI browser failure: root cause and evidence

The reported failure was reproduced from CI evidence instead of guesswork. Raw
job logs and artifacts are unreachable from this network
(`results-receiver.actions.githubusercontent.com`, `*.blob.core.windows.net`
both refuse the connection), so the smoke test was first made diagnosable: it
prints a checkpoint per phase, reports browser channel, page/console errors,
failed requests, layout numbers and a screenshot, and the runner turns the test
log tail into check annotations (readable through `api.github.com`) plus an
uploaded artifact.

Annotations from run `38434082831` identified the fault exactly:

```
error: Search for a sample location returns exactly one row / 5 !== 1
```

The test typed a search term and read `.incident-row` count immediately, racing
the re-fetch: CI measured the unfiltered sample list, the slower local machine
measured the filtered list. The count is now awaited deterministically
(`waitForRowCount`) and the row content is still asserted — the check was fixed,
not weakened. CI has been green on every run since.

### GitHub Pages

Verified locally that the Pages workflow build (`--base=/AI-Disaster-Assistant/`)
emits correct project-path asset URLs and the application build stays at `/`.
The hosted static build has no API — `/api/*` returns a GitHub Pages HTML 404 —
so the frontend now detects the unreachable backend and states it plainly, with
unknown statistics, no sample shortcut and no fabricated records. `github.io`
itself cannot be fetched from this sandbox, so the rendered public page is
**not** independently verified here.

### Still unverified (unchanged)

* Live USGS (or any external feed) fetch: blocked by sandbox egress rules;
  adapter tests use mocked HTTP responses.
* Real SMTP delivery, browser push, SMS and WhatsApp: no credentials or
  providers configured. Only the outbox logic and a fake SMTP interface are
  exercised.
* Docker/Compose build and run: no Docker daemon available.
* No durable public deployment, HTTPS/cookie verification, load test,
  penetration test, accessibility audit or disaster-recovery rehearsal.
* No measured latency, uptime or classification accuracy — none is claimed.

## 2026-10-13 — pilot implementation (previous session, superseded above)

| Check | Actual result |
|---|---|
| `python -m pytest -q` | 43 passed |
| `npm run build --prefix web` | Passed |
| `npm run test:browser --prefix web` | Passed locally |
| `pip-audit -r requirements.lock`, `npm audit --prefix web` | Clean |
| `git diff --check`, Python compile check | Passed |
| Preview HTTP smoke (`/api/health`) | `{"status":"ok","version":"0.1.0"}` |

Backend coverage: ownership of locations/notifications, unauthenticated/admin
access denial, CSRF origin/header checks, registration/logout/login, password
salt verification, input/body validation, duplicate suppression, sample
isolation, geographic matching, freshness/severity/trust gates, official
revision handling, community grouping without trust escalation, aftershock
separation, failed and malformed source handling, audit/review/resolution,
consent withdrawal, retry and mocked SMTP acceptance.
