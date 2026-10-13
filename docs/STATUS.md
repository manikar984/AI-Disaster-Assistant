# Status board

Last updated 2026-10-13, branch `arena/0aaecd40-ai-disaster-assistant`
(PR [#2](https://github.com/manikar984/AI-Disaster-Assistant/pull/2)).
Every line is backed by a command in `docs/VERIFICATION.md`; anything not
verified there is marked as not claimed.

## Definition of done

| Criterion | Status | Evidence / what is missing |
|---|---|---|
| Live ingestion from a real source | **Implemented, not yet observed live** | USGS, GDACS, RSS/Atom and CAP adapters with per-source health, counters and backoff; verified end-to-end against a local HTTP fixture server (`tests/test_sources.py`, 13 tests). Outbound access to the real hosts is blocked in this sandbox, so each source must be enabled and polled once on the deployment host (`python -m backend.cli poll --source usgs`). |
| One notification channel proven end-to-end | **Half proven — mechanism agreed (provider SMTP in the host environment)** | The email path is proven over a real SMTP conversation in all three transport modes, including implicit TLS (STARTTLS, `ssl`, `plain`; AUTH, MIME, retries, cancellation and redaction; 13 tests). The provider relay settings and sender/DKIM checklist are in `docs/DEPLOYMENT.md` step 7, with Brevo as the worked example only. Delivery to a human inbox is **not claimed** until `email-check` delivers to the owner's mailbox on the host. |
| Public HTTPS deployment | **Approved: Oracle Cloud Always Free — waiting for the owner to provision the instance** | The owner chose Oracle Cloud Always Free (`docs/HOSTING.md` keeps the comparison behind that decision; it is the only $0 option with an always-on worker and a persistent disk). No resource is created and no cost is incurred by me. Everything needed to deploy is committed: `deploy/` (systemd units, Caddy config, provisioning script, tested nightly backup), `docs/DEPLOYMENT.md` (11 steps) and the release gate `deploy/verify-live.sh`, which the CI smoke run executes against a live server on every push. |
| Protection of private data | **Implemented, reviewable** | scrypt password hashing, HttpOnly/SameSite=Strict sessions, per-user row scoping, ownership checks, CSRF origin + header checks, 20 KB body cap, rate limits, no secrets in the frontend bundle, redacted mail errors, nightly tested backups, and the retention policy is now **enforced** by a tested purger (`backend/retention.py`, `cli retention`) rather than only documented. Not measured: penetration test, MFA, distributed throttling. |

## Delivered on this branch

| Commit | What it delivered |
|---|---|
| `9de35bd`–`20d1969` | Diagnosable CI browser smoke, the real root cause of the flaky step fixed (not weakened), static-preview honesty on GitHub Pages, Pages asset-path assertion |
| `b154499` | `docs/AUDIT.md`, `docs/VERIFICATION.md` |
| `b7e5c00` | **Milestone 4 — ingestion breadth**: source registry, USGS/GDACS/RSS/CAP adapters, `/api/ready`, `/api/policy`, per-source health, corroboration rule, CLI `sources`/`poll`, 13 new tests |
| `f414c8e` | Dashboard surfaces source health, readiness, policy and evidence |
| `95fc7ff` | Geographic-precision defect found in the live pilot and fixed (migration 003 + API guard + tests) |
| `0a93cca` | **Email channel provable**: `backend/mailer.py`, `email-check` command, 8 real-SMTP tests, `docs/OPERATIONS.md` proof procedure |
| `b154499`-era docs | `docs/HOSTING.md` recommendation, this status board, README/`.env.example` refresh |
| (previous commit) | **Deployment assets for the approved host**: `deploy/` (provisioning script, systemd units, Caddy config, tested backup script) and `docs/DEPLOYMENT.md` |
| `ca40277` | **Account security**: email confirmation, single-use expiring recovery links, session listing, revoke-all, idle expiry, 12 new tests and the matching UI (banner, resend, forgot/reset, sign-out-everywhere, session list) |
| `31cdd1b` | **Email channel proven in every transport mode** (implicit TLS added, cancellation gates, 5 new tests) and **`deploy/verify-live.sh`**, the release gate that CI now runs against a live server on every push |
| `41ce4dc` | Docs reconciled with the owner's decisions (Oracle Cloud Always Free, provider SMTP in the host environment file) and the new evidence |
| (this commit) | **Retention enforced**: `backend/retention.py`, worker daily purge, `cli retention [--dry-run]`, 5 tests |

Test suite: **99 passed** (`python -m pytest -q`). Browser smokes: full-stack (including the
confirmation banner and recovery entry point) and static-preview both pass locally and in CI.

## Waiting on the owner

1. **Provision the Oracle Cloud Always Free instance** — decide: account
   creation, phone/card verification, the `VM.Standard.A1.Flex` instance and its
   SSH access are the owner's steps (₹0; nothing is billed by me). Region
   `ap-hyderabad-1` is closest; use whatever region actually has A1 capacity.
2. **SMTP credentials in the host environment** — put the provider's
   `SMTP_HOST`/`SMTP_PORT`/`SMTP_SECURITY`/`SMTP_USER`/`SMTP_PASSWORD`/`SMTP_FROM`
   into `/etc/disaster-assistant/env` (never in chat, never in git). On the host,
   `python -m backend.cli email-check --to <mailbox>` is what turns "configured"
   into "proven end-to-end"; the arrival of that message is the acceptance test.
3. **Decision on the X/social adapter** — it stays unwired until a paid plan is
   approved; the previously public bearer token must be revoked by the owner.

## Next implementable work (no owner input needed)

Ordered by risk closed per unit of effort, from `docs/AUDIT.md` §9:

1. Content-Security-Policy and `frame-ancestors` headers with a documented
   threat-model update (the proxy already sends `frame-ancestors 'none'`).
2. A restore drill test: restore a database from a `deploy/backup.sh` snapshot
   and verify row counts, not just the snapshot's own integrity (retention is now
   enforced and the backup script is tested; the drill is still manual).
3. Live updates over SSE with reconnect/backoff and a polling fallback, plus
   explicit stale-data indicators in the feed.
4. Administration: incident merge/split, type correction, moderation queue,
   delivery replay — each audited.
5. Frontend: incident routes, editable saved locations, place search, map
   clustering, accessibility and keyboard-navigation tests.
6. Remove the registration enumeration leak (answer 201 and email the existing
   holder instead of 409) once the product copy is updated.

## Explicitly not claimed

No public deployment exists yet, no live feed has been fetched from this
environment, no message has been delivered to a human inbox by a provider, no
SMS/WhatsApp/push channel works, no load, penetration, accessibility or
disaster-recovery test has been run, and nothing here is an official emergency
warning service.
