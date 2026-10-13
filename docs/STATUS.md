# Status board

Last updated 2026-10-13, branch `arena/0aaecd40-ai-disaster-assistant`
(PR [#2](https://github.com/manikar984/AI-Disaster-Assistant/pull/2)).
Every line is backed by a command in `docs/VERIFICATION.md`; anything not
verified there is marked as not claimed.

## Definition of done

| Criterion | Status | Evidence / what is missing |
|---|---|---|
| Live ingestion from a real source | **Implemented, not yet observed live** | USGS, GDACS, RSS/Atom and CAP adapters with per-source health, counters and backoff; verified end-to-end against a local HTTP fixture server (`tests/test_sources.py`, 13 tests). Outbound access to the real hosts is blocked in this sandbox, so each source must be enabled and polled once on the deployment host (`python -m backend.cli poll --source usgs`). |
| One notification channel proven end-to-end | **Half proven — provider chosen (Brevo)** | The email path is proven over a real SMTP conversation (STARTTLS, AUTH, MIME, retries, redaction) and has an operator command that proves a live deployment. Brevo relay settings and the sender/DKIM checklist are in `docs/DEPLOYMENT.md` step 7. Delivery to a human inbox is **not claimed** until `email-check` delivers to the owner's mailbox. |
| Public HTTPS deployment | **Awaiting the owner's host decision** | `docs/HOSTING.md` compares Oracle Cloud Always Free, Fly.io (Mumbai), DigitalOcean (Bangalore), Hetzner, Render, Railway and Koyeb on the three things this app cannot do without (always-on worker, persistent disk, HTTPS) and recommends Oracle Always Free at $0. No resource is created and no cost is incurred until the owner approves. Once approved: `deploy/` (systemd units, Caddy config, provisioning script, tested nightly backup) and `docs/DEPLOYMENT.md` are ready to run. |
| Protection of private data | **Implemented, reviewable** | scrypt password hashing, HttpOnly/SameSite=Strict sessions, per-user row scoping, ownership checks, CSRF origin + header checks, 20 KB body cap, rate limits, no secrets in the frontend bundle, redacted mail errors, documented retention/backup policy. Not measured: penetration test, MFA, distributed throttling. |

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
| (this commit) | **Account security**: email confirmation, single-use expiring recovery links, session listing, revoke-all, idle expiry, 12 new tests and the matching UI (banner, resend, forgot/reset, sign-out-everywhere, session list) |

Test suite: **86 passed** (`python -m pytest -q`). Browser smokes: full-stack (including the
confirmation banner and recovery entry point) and static-preview both pass locally and in CI.

## Waiting on the owner

1. **Hosting approval** — Oracle Cloud Always Free (recommended, ₹0) or a paid
   fallback (Hetzner ≈ ₹430/month, Fly.io ≈ $2–4/month). Account creation,
   verification and the credit card are the owner's steps; no credentials or
   card details ever go through chat. See `docs/HOSTING.md`.
2. **Host decision + SMTP credentials** — pick Oracle Always Free (recommended,
   $0) or a paid fallback from `docs/HOSTING.md`; then create the account and put
   the SMTP credentials in the host's environment (never in chat). On the host,
   `python -m backend.cli email-check --to <mailbox>` is what turns "configured"
   into "proven end-to-end".
3. **Decision on the X/social adapter** — it stays unwired until a paid plan is
   approved; the previously public bearer token must be revoked by the owner.

## Next implementable work (no owner input needed)

Ordered by risk closed per unit of effort, from `docs/AUDIT.md` §9:

1. Content-Security-Policy and `frame-ancestors` headers with a documented
   threat-model update (the proxy already sends `frame-ancestors 'none'`).
2. Retention/backup/restore tooling with a test that restores a database from a
   backup and verifies row counts (the nightly snapshot script itself is tested).
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
